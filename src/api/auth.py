from fastapi import APIRouter, HTTPException, status
from sqlalchemy import or_, select

from src.core.deps import CurrentUser, DbSession
from src.core.security import create_access_token, hash_password, verify_password
from src.models.enums import UserRole
from src.models.people import Patient
from src.models.user import User
from src.schemas.healthcare import (
    LoginRequest,
    LoginResponse,
    PatientCreate,
    PatientResponse,
    UserResponse,
)
from src.services.qr import generate_uhid

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: DbSession):
    """Authenticate user and return access token."""
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()

    # Same message either way - do not leak which emails are registered.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated")

    token, expires_in = create_access_token(user.id, user.role.value)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.model_validate(user),
        expires_in=expires_in,
    )


@router.post("/register/patient", response_model=PatientResponse, status_code=status.HTTP_201_CREATED)
async def register_patient(payload: PatientCreate, db: DbSession):
    """Self-service patient registration. Every other role is created by an admin."""
    email = payload.email.lower()
    username = email.split("@")[0][:100]

    existing = await db.execute(select(User).where(or_(User.email == email, User.username == username)))
    if existing.scalar_one_or_none() is not None:
        # Username is derived from the email, so a clash means the email is taken
        # or the local-part collides; disambiguate the username instead of failing.
        clash = await db.execute(select(User).where(User.email == email))
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        suffix = 1
        base = username
        while True:
            username = f"{base}{suffix}"[:100]
            probe = await db.execute(select(User).where(User.username == username))
            if probe.scalar_one_or_none() is None:
                break
            suffix += 1

    user = User(
        email=email,
        username=username,
        full_name=f"{payload.first_name} {payload.last_name}",
        hashed_password=hash_password(payload.password),
        role=UserRole.PATIENT,
        is_active=True,
    )
    db.add(user)
    await db.flush()

    # The registration form sends `allergies`/`medical_history` alongside the
    # canonical fields; fold them in rather than dropping them.
    allergies = payload.medical_allergies or payload.allergies
    conditions = payload.chronic_conditions or payload.medical_history

    patient = Patient(
        user_id=user.id,
        uhid=await generate_uhid(db),
        first_name=payload.first_name,
        last_name=payload.last_name,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        blood_group=payload.blood_group,
        phone=payload.phone,
        address=payload.address,
        emergency_contact_name=payload.emergency_contact_name,
        emergency_contact_phone=payload.emergency_contact_phone,
        medical_allergies=allergies,
        chronic_conditions=conditions,
    )
    db.add(patient)
    await db.commit()
    await db.refresh(patient)

    return PatientResponse.model_validate(patient)


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(user: CurrentUser):
    """Return the authenticated user. The frontend calls this on every page load
    to re-validate the cookie it hydrated from."""
    return UserResponse.model_validate(user)
