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
        sub = payload.get("sub")
        shop_claim = payload.get("shop_id")
        if isinstance(sub, bool) or not isinstance(sub, (str, int)):
            raise pyjwt.InvalidTokenError("invalid sub")
        if isinstance(sub, str) and (not sub.isascii() or not sub.isdigit()):
            raise pyjwt.InvalidTokenError("invalid sub")
        user_id = int(sub)
        if user_id <= 0:
            raise pyjwt.InvalidTokenError("invalid sub")
        if not isinstance(shop_claim, str):
            raise pyjwt.InvalidTokenError("invalid shop_id")
        shop_id = shop_claim.strip()
        if not shop_id:
            raise pyjwt.InvalidTokenError("missing shop_id")
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=401, detail="无效的Token")
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token已过期")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的Token")

    try:
        return get_user_by_id(repositories.users, user_id, shop_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="用户不存在")


@router.post("/register", response_model=UserResponse)
async def register(req: UserRegister, repositories: Repositories = Depends(get_repositories)):
    try:
        user = register_user(repositories.users, req.shop_id, req.username, req.password)
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login", response_model=TokenResponse)
async def login(req: UserLogin, repositories: Repositories = Depends(get_repositories)):
    try:
        user = authenticate_user(repositories.users, req.shop_id, req.username, req.password)
        token = create_access_token({"sub": str(user.id), "shop_id": user.shop_id})
        return TokenResponse(access_token=token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user_dependency)):
    return current_user
