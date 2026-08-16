from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import decode_token
from src.db.session import get_db
from src.models.enums import UserRole
from src.models.organizations import DiagnosticCenter, Pharmacy
from src.models.people import Doctor, Patient
from src.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is deactivated")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable:
    async def _guard(user: CurrentUser) -> User:
        if user.role not in roles:
            allowed = ", ".join(r.value for r in roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires one of the following roles: {allowed}",
            )
        return user

    return _guard


async def get_current_patient(
    user: Annotated[User, Depends(require_role(UserRole.PATIENT))],
    db: DbSession,
) -> Patient:
    result = await db.execute(select(Patient).where(Patient.user_id == user.id))
    patient = result.scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient profile not found")
    return patient


async def get_current_doctor(
    user: Annotated[User, Depends(require_role(UserRole.DOCTOR))],
    db: DbSession,
) -> Doctor:
    result = await db.execute(select(Doctor).where(Doctor.user_id == user.id))
    doctor = result.scalar_one_or_none()
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor profile not found")
    return doctor


async def get_current_pharmacy(
    user: Annotated[User, Depends(require_role(UserRole.PHARMACY))],
    db: DbSession,
) -> Pharmacy:
    result = await db.execute(select(Pharmacy).where(Pharmacy.user_id == user.id))
    pharmacy = result.scalar_one_or_none()
    if pharmacy is None:
        raise HTTPException(status_code=404, detail="Pharmacy profile not found")
    return pharmacy


async def get_current_diagnostic_center(
    user: Annotated[User, Depends(require_role(UserRole.DIAGNOSTIC_CENTER))],
    db: DbSession,
) -> DiagnosticCenter:
    result = await db.execute(select(DiagnosticCenter).where(DiagnosticCenter.user_id == user.id))
    center = result.scalar_one_or_none()
    if center is None:
        raise HTTPException(status_code=404, detail="Diagnostic center profile not found")
    return center


CurrentPatient = Annotated[Patient, Depends(get_current_patient)]
CurrentDoctor = Annotated[Doctor, Depends(get_current_doctor)]
CurrentPharmacy = Annotated[Pharmacy, Depends(get_current_pharmacy)]
CurrentDiagnosticCenter = Annotated[DiagnosticCenter, Depends(get_current_diagnostic_center)]
AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
