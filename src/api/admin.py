from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from src.core.deps import AdminUser, DbSession
from src.core.security import hash_password
from src.models.enums import QueueStatus, UserRole
from src.models.organizations import DiagnosticCenter, Hospital, Pharmacy
from src.models.people import Doctor, Nurse, Patient
from src.models.queue import QueueEntry
from src.models.user import User
from src.schemas.healthcare import (
    AdminStatsResponse,
    DiagnosticCenterCreate,
    DiagnosticCenterResponse,
    DoctorCreate,
    DoctorResponse,
    HospitalCreate,
    HospitalResponse,
    NurseCreate,
    NurseResponse,
    PharmacyCreate,
    PharmacyResponse,
    UserResponse,
)

router = APIRouter(prefix="/api/admin", tags=["Admin"], dependencies=[])


async def _count(db: DbSession, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for clause in where:
        stmt = stmt.where(clause)
    return int((await db.execute(stmt)).scalar_one())


@router.get("/stats", response_model=AdminStatsResponse)
async def platform_stats(admin: AdminUser, db: DbSession):
    """Live platform numbers for the admin overview."""
    today = date.today()
    week_ago = datetime.now(UTC) - timedelta(days=7)

    return AdminStatsResponse(
        total_users=await _count(db, User),
        active_users=await _count(db, User, User.is_active.is_(True)),
        patients=await _count(db, Patient),
        doctors=await _count(db, Doctor),
        nurses=await _count(db, Nurse),
        pharmacies=await _count(db, Pharmacy),
        hospitals=await _count(db, Hospital),
        diagnostic_centers=await _count(db, DiagnosticCenter),
        consultations_today=await _count(
            db,
            QueueEntry,
            QueueEntry.status == QueueStatus.COMPLETED,
            func.date(QueueEntry.consultation_completed_at) == today,
        ),
        patients_waiting=await _count(db, QueueEntry, QueueEntry.status == QueueStatus.WAITING),
        registrations_this_week=await _count(db, User, User.created_at >= week_ago),
    )


async def _create_user_for_org(
    db: DbSession, email: str, full_name: str, password: str, role: UserRole
) -> User:
    email = email.lower()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    username = email.split("@")[0][:100]
    probe = await db.execute(select(User).where(User.username == username))
    if probe.scalar_one_or_none() is not None:
        suffix = 1
        base = username
        while True:
            username = f"{base}{suffix}"[:100]
            check = await db.execute(select(User).where(User.username == username))
            if check.scalar_one_or_none() is None:
                break
            suffix += 1

    user = User(
        email=email,
        username=username,
        full_name=full_name,
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def _assert_unique_license(db: DbSession, model, license_number: str) -> None:
    existing = await db.execute(select(model).where(model.license_number == license_number))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="That license number is already registered")


# --------------------------------------------------------------------------
# Hospitals
# --------------------------------------------------------------------------
@router.get("/hospitals", response_model=list[HospitalResponse])
async def get_hospitals(admin: AdminUser, db: DbSession):
    result = await db.execute(select(Hospital).order_by(Hospital.name))
    return [HospitalResponse.model_validate(h) for h in result.scalars().all()]


@router.post("/hospitals", response_model=HospitalResponse, status_code=status.HTTP_201_CREATED)
async def create_hospital(payload: HospitalCreate, admin: AdminUser, db: DbSession):
    await _assert_unique_license(db, Hospital, payload.license_number)
    hospital = Hospital(**payload.model_dump())
    db.add(hospital)
    await db.commit()
    await db.refresh(hospital)
    return HospitalResponse.model_validate(hospital)


# --------------------------------------------------------------------------
# Doctors
# --------------------------------------------------------------------------
@router.get("/doctors", response_model=list[DoctorResponse])
async def get_doctors(admin: AdminUser, db: DbSession):
    result = await db.execute(select(Doctor, User).join(User, Doctor.user_id == User.id).order_by(Doctor.id))
    payload = []
    for doctor, user in result.all():
        item = DoctorResponse.model_validate(doctor)
        item.full_name = user.full_name
        item.email = user.email
        payload.append(item)
    return payload


@router.post("/doctors", response_model=DoctorResponse, status_code=status.HTTP_201_CREATED)
async def create_doctor(payload: DoctorCreate, admin: AdminUser, db: DbSession):
    if await db.get(Hospital, payload.hospital_id) is None:
        raise HTTPException(status_code=404, detail="Hospital not found")
    await _assert_unique_license(db, Doctor, payload.license_number)

    user = await _create_user_for_org(db, payload.email, payload.full_name, payload.password, UserRole.DOCTOR)
    doctor = Doctor(
        user_id=user.id,
        hospital_id=payload.hospital_id,
        license_number=payload.license_number,
        specialization=payload.specialization,
        years_of_experience=payload.years_of_experience,
        qualification=payload.qualification,
        consultation_fee=payload.consultation_fee,
    )
    db.add(doctor)
    await db.commit()
    await db.refresh(doctor)

    item = DoctorResponse.model_validate(doctor)
    item.full_name = payload.full_name
    item.email = payload.email.lower()
    return item


# --------------------------------------------------------------------------
# Nurses
# --------------------------------------------------------------------------
@router.get("/nurses", response_model=list[NurseResponse])
async def get_nurses(admin: AdminUser, db: DbSession):
    result = await db.execute(select(Nurse).order_by(Nurse.id))
    return [NurseResponse.model_validate(n) for n in result.scalars().all()]


@router.post("/nurses", response_model=NurseResponse, status_code=status.HTTP_201_CREATED)
async def create_nurse(payload: NurseCreate, admin: AdminUser, db: DbSession):
    if await db.get(Hospital, payload.hospital_id) is None:
        raise HTTPException(status_code=404, detail="Hospital not found")
    await _assert_unique_license(db, Nurse, payload.license_number)

    user = await _create_user_for_org(db, payload.email, payload.full_name, payload.password, UserRole.NURSE)
    nurse = Nurse(
        user_id=user.id,
        hospital_id=payload.hospital_id,
        license_number=payload.license_number,
        department=payload.department,
        shift=payload.shift,
        years_of_experience=payload.years_of_experience,
    )
    db.add(nurse)
    await db.commit()
    await db.refresh(nurse)
    return NurseResponse.model_validate(nurse)


# --------------------------------------------------------------------------
# Pharmacies
# --------------------------------------------------------------------------
@router.get("/pharmacies", response_model=list[PharmacyResponse])
async def get_pharmacies(admin: AdminUser, db: DbSession):
    result = await db.execute(select(Pharmacy).order_by(Pharmacy.name))
    return [PharmacyResponse.model_validate(p) for p in result.scalars().all()]


@router.post("/pharmacies", response_model=PharmacyResponse, status_code=status.HTTP_201_CREATED)
async def create_pharmacy(payload: PharmacyCreate, admin: AdminUser, db: DbSession):
    await _assert_unique_license(db, Pharmacy, payload.license_number)
    user = await _create_user_for_org(db, payload.email, payload.name, payload.password, UserRole.PHARMACY)
    pharmacy = Pharmacy(
        user_id=user.id,
        name=payload.name,
        license_number=payload.license_number,
        address=payload.address,
        phone=payload.phone,
        email=payload.email.lower(),
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    db.add(pharmacy)
    await db.commit()
    await db.refresh(pharmacy)
    return PharmacyResponse.model_validate(pharmacy)


# --------------------------------------------------------------------------
# Diagnostic centers
# --------------------------------------------------------------------------
@router.get("/diagnostic-centers", response_model=list[DiagnosticCenterResponse])
async def get_diagnostic_centers(admin: AdminUser, db: DbSession):
    result = await db.execute(select(DiagnosticCenter).order_by(DiagnosticCenter.name))
    return [DiagnosticCenterResponse.model_validate(c) for c in result.scalars().all()]


@router.post(
    "/diagnostic-centers",
    response_model=DiagnosticCenterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_diagnostic_center(payload: DiagnosticCenterCreate, admin: AdminUser, db: DbSession):
    await _assert_unique_license(db, DiagnosticCenter, payload.license_number)
    user = await _create_user_for_org(
        db, payload.email, payload.name, payload.password, UserRole.DIAGNOSTIC_CENTER
    )
    center = DiagnosticCenter(
        user_id=user.id,
        name=payload.name,
        license_number=payload.license_number,
        address=payload.address,
        phone=payload.phone,
        email=payload.email.lower(),
        services_offered=payload.services_offered,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    db.add(center)
    await db.commit()
    await db.refresh(center)
    return DiagnosticCenterResponse.model_validate(center)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
@router.get("/users", response_model=list[UserResponse])
async def get_all_users(admin: AdminUser, db: DbSession, skip: int = 0, limit: int = 100):
    result = await db.execute(select(User).order_by(User.id).offset(skip).limit(min(limit, 500)))
    return [UserResponse.model_validate(u) for u in result.scalars().all()]


@router.put("/users/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(user_id: int, admin: AdminUser, db: DbSession):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.put("/users/{user_id}/activate", response_model=UserResponse)
async def activate_user(user_id: int, admin: AdminUser, db: DbSession):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = True
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)
