import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import (
    admin,
    ai,
    auth,
    consultation,
    doctor,
    doctor_ops,
    documents,
    events,
    patients,
    pharmacy,
    queue,
    v1_users,
)
from src.core.config import settings
from src.core.errors import register_exception_handlers
from src.services.events import close_events
from src.services.queue.rescorer import start_rescorer, stop_rescorer

logging.basicConfig(level=logging.INFO if not settings.debug else logging.DEBUG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("MediLink API starting up")
    start_rescorer()
    yield
    await stop_rescorer()
    await close_events()

    from src.db.session import engine

    await engine.dispose()
    logger.info("MediLink API shut down")


app = FastAPI(
    title="MediLink Backend",
    description="MediLink Backend API - A comprehensive healthcare management system",
    version=settings.app_version,
    lifespan=lifespan,
)

# The frontend sends `withCredentials: false` and authenticates with a bearer
# header, so credentials must stay off here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(v1_users.router)
app.include_router(auth.router)
app.include_router(patients.router)
app.include_router(admin.router)
app.include_router(doctor.router)
app.include_router(pharmacy.router)
app.include_router(ai.router)
app.include_router(queue.router)
app.include_router(events.router)
app.include_router(doctor_ops.router)
app.include_router(doctor_ops.pharmacy_router)
app.include_router(documents.router)
app.include_router(consultation.router)


@app.get("/", tags=["root"])
async def root():
    return {
        "name": "MediLink Backend",
        "version": settings.app_version,
        "docs": "/docs",
    }
