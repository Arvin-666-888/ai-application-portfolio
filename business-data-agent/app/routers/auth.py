import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.dependencies import get_repositories
from app.models.models import User
from app.repositories import Repositories
from app.schemas.schemas import UserRegister, UserLogin, TokenResponse, UserResponse
from app.services.auth_service import (
    register_user, authenticate_user, create_access_token, get_user_by_id,
)
import jwt as pyjwt

logger = logging.getLogger("kb_qa.auth_router")

router = APIRouter(prefix="/api/auth", tags=["认证"])
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user_dependency(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    repositories: Repositories = Depends(get_repositories),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="无效的认证头")

    token = credentials.credentials
    try:
        payload = pyjwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = int(payload.get("sub"))
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token已过期")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的Token")

    try:
        return get_user_by_id(repositories.users, user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="用户不存在")


@router.post("/register", response_model=UserResponse)
async def register(req: UserRegister, repositories: Repositories = Depends(get_repositories)):
    try:
        user = register_user(repositories.users, req.username, req.password)
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login", response_model=TokenResponse)
async def login(req: UserLogin, repositories: Repositories = Depends(get_repositories)):
    try:
        user = authenticate_user(repositories.users, req.username, req.password)
        token = create_access_token({"sub": str(user.id)})
        return TokenResponse(access_token=token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user_dependency)):
    return current_user
