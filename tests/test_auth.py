async def test_health_check(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert "timestamp" in body


async def test_register_patient_allocates_uhid(client, patient_payload):
    resp = await client.post("/api/auth/register/patient", json=patient_payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["uhid"].startswith("ML")
    assert len(body["uhid"]) == 12
    assert body["first_name"] == "Test"
    # A registration response must never echo credentials back.
    assert "password" not in body
    assert "hashed_password" not in body


async def test_register_duplicate_email_is_rejected(client, patient_payload):
    first = await client.post("/api/auth/register/patient", json=patient_payload)
    assert first.status_code == 201
    second = await client.post("/api/auth/register/patient", json=patient_payload)
    assert second.status_code == 409
    assert isinstance(second.json()["detail"], str)


async def test_login_returns_token_and_user(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    resp = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["role"] == "patient"
    assert body["user"]["email"] == patient_payload["email"]


async def test_login_with_wrong_password_is_401(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    resp = await client.post(
        "/api/auth/login", json={"email": patient_payload["email"], "password": "WrongPass123"}
    )
    assert resp.status_code == 401


async def test_login_for_unknown_email_gives_same_error_as_wrong_password(client):
    resp = await client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "Whatever123"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_me_requires_authentication(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_rejects_a_garbage_token(client):
    resp = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


async def test_me_returns_the_logged_in_user(client, patient_payload):
    await client.post("/api/auth/register/patient", json=patient_payload)
    login = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    token = login.json()["access_token"]
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == patient_payload["email"]


async def test_validation_error_uses_the_shape_the_frontend_parses(client):
    """The axios interceptor renders detail[].msg - a different shape shows an
    empty toast, so the contract matters as much as the status code."""
    resp = await client.post("/api/auth/login", json={"email": "not-an-email"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert {"loc", "msg", "type"} <= set(detail[0])


async def test_deactivated_user_cannot_log_in(client, patient_payload, session_factory):
    from sqlalchemy import select

    from src.models.user import User

    await client.post("/api/auth/register/patient", json=patient_payload)
    async with session_factory() as db:
        user = (await db.execute(select(User).where(User.email == patient_payload["email"]))).scalar_one()
        user.is_active = False
        await db.commit()

    resp = await client.post(
        "/api/auth/login",
        json={"email": patient_payload["email"], "password": patient_payload["password"]},
    )
    assert resp.status_code == 403
