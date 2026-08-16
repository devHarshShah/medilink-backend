"""SSE auth and token handling.

The stream itself needs Redis, so these cover the parts that must be right
regardless: who is allowed to open a stream, and that the query-string token is
a narrow, short-lived credential rather than a bearer token in disguise.
"""

import pytest_asyncio

from src.core.security import create_access_token, create_stream_token, decode_token


@pytest_asyncio.fixture
async def patient_headers(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_stream_token_requires_authentication(client):
    assert (await client.post("/api/events/token")).status_code == 401


async def test_stream_token_is_issued_to_an_authenticated_user(client, patient_headers):
    resp = await client.post("/api/events/token", headers=patient_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    # Short-lived: it travels in a query string, which ends up in access logs.
    assert body["expires_in"] <= 300


def test_stream_tokens_are_a_separate_type_from_access_tokens():
    """A stream token must not work as a bearer token, and vice versa --
    otherwise a logged URL becomes a full credential."""
    stream = create_stream_token(1, "patient")
    access, _ = create_access_token(1, "patient")

    assert decode_token(stream, expected_type="stream") is not None
    assert decode_token(stream, expected_type="access") is None
    assert decode_token(access, expected_type="stream") is None


async def test_queue_stream_rejects_a_missing_token(client):
    assert (await client.get("/api/events/queue/1")).status_code == 422


async def test_queue_stream_rejects_a_garbage_token(client):
    resp = await client.get("/api/events/queue/1", params={"token": "not-a-token"})
    assert resp.status_code == 401


async def test_queue_stream_rejects_an_access_token(client, patient_headers):
    """Passing the ordinary bearer token in the query string must not work."""
    token = patient_headers["Authorization"].removeprefix("Bearer ")
    resp = await client.get("/api/events/queue/1", params={"token": token})
    assert resp.status_code == 401


async def test_queue_stream_404s_for_an_unknown_entry(client, patient_headers):
    token = (await client.post("/api/events/token", headers=patient_headers)).json()["token"]
    resp = await client.get("/api/events/queue/999999", params={"token": token})
    assert resp.status_code == 404


async def test_doctor_stream_rejects_a_patient(client, patient_headers):
    token = (await client.post("/api/events/token", headers=patient_headers)).json()["token"]
    resp = await client.get("/api/events/doctor/1", params={"token": token})
    assert resp.status_code == 403
