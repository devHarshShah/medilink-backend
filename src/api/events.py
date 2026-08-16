"""Server-Sent Events for live queue updates.

EventSource cannot set an Authorization header, so these endpoints take a
short-lived stream token in the query string instead. That token is minted from
a normal bearer-authenticated request, expires in 5 minutes, and is useless for
anything but reading a stream -- query strings end up in access logs, so it must
not be a long-lived credential.
"""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from starlette.responses import StreamingResponse

from src.core.deps import CurrentUser, DbSession
from src.core.security import create_stream_token, decode_token
from src.models.enums import UserRole
from src.models.people import Doctor, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.queue import StreamTokenResponse
from src.services.events import doctor_channel, queue_channel, subscribe

router = APIRouter(prefix="/api/events", tags=["Events"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx buffers by default, which silently breaks SSE.
    "X-Accel-Buffering": "no",
}


@router.post("/token", response_model=StreamTokenResponse)
async def issue_stream_token(user: CurrentUser):
    """Mint a short-lived token for EventSource to use in the query string."""
    return StreamTokenResponse(token=create_stream_token(user.id, user.role.value), expires_in=300)


async def _user_from_stream_token(db: DbSession, token: str) -> User:
    payload = decode_token(token, expected_type="stream")
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired stream token")
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid stream token")
    return user


@router.get("/queue/{queue_id}")
async def stream_queue(queue_id: int, db: DbSession, token: str = Query(...)):
    """Live position, re-assignment and call events for one queue entry."""
    user = await _user_from_stream_token(db, token)

    entry = await db.get(QueueEntry, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found")

    allowed = False
    if user.role == UserRole.PATIENT:
        row = await db.execute(select(Patient).where(Patient.user_id == user.id))
        patient = row.scalar_one_or_none()
        allowed = patient is not None and entry.patient_id == patient.id
    elif user.role == UserRole.DOCTOR:
        row = await db.execute(select(Doctor).where(Doctor.user_id == user.id))
        doctor = row.scalar_one_or_none()
        allowed = doctor is not None and entry.doctor_id == doctor.id
    elif user.role in (UserRole.ADMIN, UserRole.NURSE):
        allowed = True

    if not allowed:
        raise HTTPException(status_code=403, detail="You do not have access to this queue entry")

    return StreamingResponse(
        subscribe(queue_channel(queue_id)), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/doctor/{doctor_id}")
async def stream_doctor_queue(doctor_id: int, db: DbSession, token: str = Query(...)):
    """Live updates for a doctor's working list."""
    user = await _user_from_stream_token(db, token)

    allowed = user.role in (UserRole.ADMIN, UserRole.NURSE)
    if user.role == UserRole.DOCTOR:
        row = await db.execute(select(Doctor).where(Doctor.user_id == user.id))
        doctor = row.scalar_one_or_none()
        allowed = doctor is not None and doctor.id == doctor_id

    if not allowed:
        raise HTTPException(status_code=403, detail="You can only stream your own queue")

    return StreamingResponse(
        subscribe(doctor_channel(doctor_id)), media_type="text/event-stream", headers=SSE_HEADERS
    )
