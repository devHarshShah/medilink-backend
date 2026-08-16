import os

# Point the app at a throwaway SQLite database before any app module is imported.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.core.security import hash_password  # noqa: E402
from src.db.session import get_db  # noqa: E402
from src.main import app  # noqa: E402
from src.models import Base  # noqa: E402
from src.models.enums import UserRole  # noqa: E402
from src.models.user import User  # noqa: E402


@pytest_asyncio.fixture
async def engine():
    # StaticPool keeps a single in-memory DB alive across connections.
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def client(session_factory):
    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_user(session_factory) -> User:
    async with session_factory() as db:
        user = User(
            email="admin@example.com",
            username="admin",
            full_name="Test Admin",
            hashed_password=hash_password("AdminPass123"),
            role=UserRole.ADMIN,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest_asyncio.fixture
async def admin_headers(client, admin_user) -> dict[str, str]:
    resp = await client.post(
        "/api/auth/login", json={"email": "admin@example.com", "password": "AdminPass123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def patient_payload() -> dict:
    return {
        "first_name": "Test",
        "last_name": "Patient",
        "date_of_birth": "1995-06-15",
        "gender": "male",
        "blood_group": "O+",
        "phone": "+919999999999",
        "address": "123 Test Street, Bengaluru",
        "email": "test.patient@example.com",
        "password": "PatientPass123",
    }
