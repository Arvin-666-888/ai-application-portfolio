from fastapi import APIRouter

from app.config import settings
from app.schemas.schemas import HealthResponse


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        app=settings.APP_NAME,
        version="1.0.0",
        commerce_backend=settings.COMMERCE_BACKEND,
    )
