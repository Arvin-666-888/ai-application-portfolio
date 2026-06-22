import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import init_db
from app.routers import auth, documents, knowledge_bases, chat

logger = logging.getLogger("kb_qa")

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)


class AppException(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info(f"{settings.APP_NAME} started")
    logger.info(f"API_KEY configured: {'Yes' if settings.API_KEY else 'No (using mock mode)'}")
    yield
    logger.info(f"{settings.APP_NAME} shutdown")


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.code,
        content={"code": exc.code, "message": exc.message},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务器内部错误"},
    )


app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(knowledge_bases.router)
app.include_router(chat.router)


@app.get("/")
async def root():
    return {
        "app": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "api_key_configured": bool(settings.API_KEY),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
