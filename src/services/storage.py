"""Object storage for patient documents and lab reports.

S3-compatible (MinIO in development). Files never go through the database, and
clients never see the object key -- downloads go via a short-lived presigned
URL so a leaked link expires on its own.
"""

import logging
import uuid
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.core.config import settings

logger = logging.getLogger(__name__)

# Magic numbers, checked server-side. A client-supplied content-type is a
# claim, not evidence -- renaming a script to .pdf must not get it stored as one.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x25\x50\x44\x46", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a", "image/png"),
]

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4"),
        )
    return _client


def sniff_content_type(head: bytes) -> str | None:
    """Detect the real type from the file's own bytes."""
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type
    return None


class StorageError(RuntimeError):
    pass


def ensure_bucket() -> None:
    try:
        client().head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        try:
            client().create_bucket(Bucket=settings.s3_bucket)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError("Document storage is unavailable") from exc


def build_key(patient_id: int, filename: str) -> str:
    """Namespaced by patient, with a random component.

    The filename is never used as the key: a client-supplied name can contain
    path traversal, and two patients uploading `report.pdf` must not collide.
    """
    stamp = datetime.now(UTC).strftime("%Y/%m")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    if len(suffix) > 8 or not suffix.isalnum():
        suffix = "bin"
    return f"patients/{patient_id}/{stamp}/{uuid.uuid4().hex}.{suffix}"


def upload(key: str, data: bytes, content_type: str) -> None:
    try:
        ensure_bucket()
        client().put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Upload failed for %s", key, exc_info=True)
        raise StorageError("Could not store the file. Please try again.") from exc


def presigned_url(key: str, filename: str, expires_seconds: int = 300) -> str:
    """Short-lived download link.

    The URL carries its own authorisation, so it must expire quickly -- it will
    end up in browser history and referrer headers.
    """
    try:
        url = client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=expires_seconds,
        )
    except (ClientError, BotoCoreError) as exc:
        raise StorageError("Could not generate a download link") from exc

    # Inside Docker the API talks to `minio:9000`, but the browser cannot
    # resolve that. Swap in the externally reachable host.
    if settings.s3_public_endpoint_url != settings.s3_endpoint_url:
        url = url.replace(settings.s3_endpoint_url, settings.s3_public_endpoint_url, 1)
    return url


def delete(key: str) -> None:
    try:
        client().delete_object(Bucket=settings.s3_bucket, Key=key)
    except (ClientError, BotoCoreError):
        # The database row is the source of truth for what a patient can see.
        # An orphaned object is a cleanup problem, not a correctness one.
        logger.warning("Could not delete object %s", key, exc_info=True)
