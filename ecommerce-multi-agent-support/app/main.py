import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.config import PROJECT_ROOT, settings
from app.database import SessionLocal, init_db
from app.routers import auth, chat, commerce, health, routing
from app.services.seed_service import seed_demo_data


logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("voltcore")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    init_db()
    if settings.SEED_DEMO_DATA:
        with SessionLocal() as db:
            counts = seed_demo_data(db, seed=settings.DEMO_DATA_SEED)
        logger.info("Demo data ready: %s", counts)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="Runnable and evaluated V1.0 LangGraph multi-agent ecommerce support system.",
    lifespan=lifespan,
)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(commerce.router)
app.include_router(routing.router)
app.include_router(chat.router)
