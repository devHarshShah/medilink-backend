from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import or_, select

from src.core.deps import AdminUser, DbSession
from src.core.security import hash_password
from src.models.user import User
from src.schemas.user import HealthCheckResponse, UserCreate, UserResponse, UserUpdate

router = APIRouter(prefix="/api/v1", tags=["users"])


@router.get("/health", response_model=HealthCheckResponse, tags=["users", "health"])
async def health_check(db: DbSession):
    """Liveness plus a real database round-trip - a health check that never
    touches the DB will happily report green while every request 500s."""
    from sqlalchemy import text

    try:
        await db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False

    return HealthCheckResponse(
        status="healthy" if database_ok else "degraded",
        message="MediLink API is running" if database_ok else "Database is unreachable",
        timestamp=datetime.now(UTC),
    )


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, admin: AdminUser, db: DbSession):
    existing = await db.execute(
        select(User).where(or_(User.email == payload.email.lower(), User.username == payload.username))
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="That email or username is already taken")

    user = User(
        email=payload.email.lower(),
        username=payload.username,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.get("/users", response_model=list[UserResponse])
async def get_users(admin: AdminUser, db: DbSession, skip: int = 0, limit: int = 100):
    result = await db.execute(select(User).order_by(User.id).offset(skip).limit(min(limit, 500)))
    return [UserResponse.model_validate(u) for u in result.scalars().all()]


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, admin: AdminUser, db: DbSession):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.model_validate(user)


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(user_id: int, payload: UserUpdate, admin: AdminUser, db: DbSession):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    data = payload.model_dump(exclude_unset=True)
    if "password" in data and data["password"]:
        user.hashed_password = hash_password(data.pop("password"))
    else:
        data.pop("password", None)
    if "email" in data and data["email"]:
        data["email"] = data["email"].lower()

    for field, value in data.items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, admin: AdminUser, db: DbSession):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.commit()
