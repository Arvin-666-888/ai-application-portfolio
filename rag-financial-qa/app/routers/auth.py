import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.schemas.schemas import UserRegister, UserLogin, TokenResponse, UserResponse
from app.services.auth_service import (
    register_user, authenticate_user, create_access_token, get_user_by_id,
)

logger = logging.getLogger("kb_qa.auth_router")

router = APIRouter(prefix="/api/auth", tags=["认证"])
security = HTTPBearer()


async def get_current_user(
    token: str = None,
    db: Session = Depends(get_db),
) -> User:
    from fastapi import Request
    raise HTTPException(status_code=401, detail="请使用 get_current_user_dependency")


def _decode_token(authorization: str, db: Session) -> User:
    import jwt as pyjwt
    from app.config import settings

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="无效的认证头")

    token = authorization[7:]
    try:
        payload = pyjwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_sub": False},
        )
        user_id = int(payload.get("sub"))
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token已过期")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的Token")

    try:
        return get_user_by_id(db, user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="用户不存在")


async def get_current_user_dependency(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    return _decode_token(f"Bearer {credentials.credentials}", db)


@router.post("/register", response_model=UserResponse)
async def register(req: UserRegister, db: Session = Depends(get_db)):
    try:
        user = register_user(db, req.username, req.password)
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login", response_model=TokenResponse)
async def login(req: UserLogin, db: Session = Depends(get_db)):
    try:
        user = authenticate_user(db, req.username, req.password)
        token = create_access_token({"sub": str(user.id)})
        return TokenResponse(access_token=token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user_dependency)):
    return current_user
