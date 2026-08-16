"""Patient document vault.

Storage is stubbed so these run without MinIO; the validation that happens
*before* any bytes are stored is the part worth pinning.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.models.consent import MedicalDocument
from src.services import storage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


@pytest_asyncio.fixture
async def ph(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture(autouse=True)
def stub_storage(monkeypatch):
    stored: dict[str, bytes] = {}
    monkeypatch.setattr(storage, "upload", lambda key, data, ct: stored.__setitem__(key, data))
    monkeypatch.setattr(storage, "delete", lambda key: stored.pop(key, None))
    monkeypatch.setattr(
        storage, "presigned_url", lambda key, filename, expires_seconds=300: f"https://x/{key}"
    )
    return stored


def _upload(client, headers, content: bytes, filename: str, **fields):
    data = {"document_type": "lab_report"}
    data.update(fields)
    return client.post(
        "/api/patients/me/documents/upload",
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
        headers=headers,
    )


async def test_upload_and_list_a_document(client, ph):
    resp = await _upload(client, ph, PDF, "results.pdf", description="Blood work")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["file_name"] == "results.pdf"
    # Uppercase on the wire -- medical-history/page.tsx compares uppercase.
    assert body["status"] == "UPLOADED"
    assert body["file_size"] == len(PDF)

    listed = (await client.get("/api/patients/me/documents", headers=ph)).json()
    assert len(listed) == 1


async def test_png_and_jpeg_are_accepted(client, ph):
    assert (await _upload(client, ph, PNG, "scan.png")).status_code == 201
    assert (await _upload(client, ph, JPEG, "photo.jpg")).status_code == 201


async def test_a_renamed_file_is_rejected_on_its_real_bytes(client, ph):
    """A client-supplied content type is a claim, not evidence: renaming a
    script to .pdf must not get it stored as one."""
    resp = await _upload(client, ph, b"#!/bin/sh\nrm -rf /\n", "totally-a.pdf")
    assert resp.status_code == 422
    assert "jpeg" in resp.json()["detail"].lower()


async def test_an_empty_file_is_rejected(client, ph):
    assert (await _upload(client, ph, b"", "empty.pdf")).status_code == 422


async def test_an_oversized_file_is_rejected(client, ph):
    big = PDF + b"\x00" * (11 * 1024 * 1024)
    resp = await _upload(client, ph, big, "huge.pdf")
    assert resp.status_code == 413


async def test_an_unknown_document_type_is_rejected(client, ph):
    resp = await _upload(client, ph, PDF, "x.pdf", document_type="not_a_real_type")
    assert resp.status_code == 422


async def test_a_bad_document_date_is_rejected(client, ph):
    resp = await _upload(client, ph, PDF, "x.pdf", document_date="last tuesday")
    assert resp.status_code == 422


async def test_the_storage_key_is_never_exposed(client, ph, session_factory):
    """The key is an internal address; clients get a presigned URL instead."""
    body = (await _upload(client, ph, PDF, "x.pdf")).json()
    assert "storage_key" not in body

    async with session_factory() as db:
        document = (await db.execute(select(MedicalDocument))).scalars().one()
    assert document.storage_key not in str(body)


async def test_the_key_does_not_use_the_client_filename(client, ph, session_factory):
    """A supplied name can contain traversal, and two patients uploading
    report.pdf must not collide."""
    await _upload(client, ph, PDF, "../../etc/passwd.pdf")
    async with session_factory() as db:
        document = (await db.execute(select(MedicalDocument))).scalars().one()
    assert ".." not in document.storage_key
    assert document.storage_key.startswith(f"patients/{document.patient_id}/")


async def test_download_redirects_to_a_presigned_url(client, ph):
    document_id = (await _upload(client, ph, PDF, "x.pdf")).json()["id"]
    resp = await client.get(f"/api/patients/me/documents/{document_id}/download", headers=ph)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://x/")


async def test_delete_removes_the_document(client, ph, stub_storage):
    document_id = (await _upload(client, ph, PDF, "x.pdf")).json()["id"]
    assert len(stub_storage) == 1

    resp = await client.delete(f"/api/patients/me/documents/{document_id}", headers=ph)
    assert resp.status_code == 204
    assert (await client.get("/api/patients/me/documents", headers=ph)).json() == []
    assert stub_storage == {}


async def test_a_patient_cannot_read_another_patients_document(client, ph):
    document_id = (await _upload(client, ph, PDF, "x.pdf")).json()["id"]

    other = {
        "first_name": "Other",
        "last_name": "Person",
        "date_of_birth": "1990-01-01",
        "gender": "female",
        "phone": "+919888888888",
        "address": "Elsewhere",
        "email": "other.docs@example.com",
        "password": "OtherPass123",
    }
    await client.post("/api/auth/register/patient", json=other)
    login = await client.post(
        "/api/auth/login", json={"email": other["email"], "password": other["password"]}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (
        await client.get(f"/api/patients/me/documents/{document_id}/download", headers=headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/patients/me/documents/{document_id}", headers=headers)
    ).status_code == 404


async def test_uploads_require_authentication(client):
    resp = await client.post(
        "/api/patients/me/documents/upload",
        files={"file": ("x.pdf", PDF, "application/pdf")},
        data={"document_type": "lab_report"},
    )
    assert resp.status_code == 401


def test_content_sniffing_recognises_the_allowed_types():
    assert storage.sniff_content_type(PDF) == "application/pdf"
    assert storage.sniff_content_type(PNG) == "image/png"
    assert storage.sniff_content_type(JPEG) == "image/jpeg"
    assert storage.sniff_content_type(b"MZ\x90\x00") is None


def test_generated_keys_are_unique_for_the_same_filename():
    a = storage.build_key(1, "report.pdf")
    b = storage.build_key(1, "report.pdf")
    assert a != b


def test_a_hostile_extension_is_normalised():
    key = storage.build_key(1, "evil.php%00.pdf")
    assert key.endswith(".pdf")
    key2 = storage.build_key(1, "noextension")
    assert key2.endswith(".bin")
