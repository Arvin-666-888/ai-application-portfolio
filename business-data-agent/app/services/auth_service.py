import logging
from datetime import datetime, timedelta

import jwt

from app.config import settings
from app.models.models import User
from app.repositories import UserRepository

logger = logging.getLogger("kb_qa.auth")

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    HAS_PASSLIB = True
except ImportError:
    import hashlib
    HAS_PASSLIB = False
    logger.warning("passlib not installed, using simple hash")


def _simple_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _simple_verify(password: str, hashed: str) -> bool:
    return _simple_hash(password) == hashed


def hash_password(password: str) -> str:
    if HAS_PASSLIB:
        return pwd_context.hash(password)
    return _simple_hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if HAS_PASSLIB:
        return pwd_context.verify(plain_password, hashed_password)
    return _simple_verify(plain_password, hashed_password)


def create_access_token(data: dict) -> str:
    # MIGRATION: 仅用户身份 JWT -> 由服务端签发 user_id + shop_id 联合租户上下文。
    to_encode = data.copy()
    expire = datetime.now() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def register_user(users: UserRepository, shop_id: str, username: str, password: str) -> User:
    existing = users.get_by_username(username, shop_id)
    if existing:
        raise ValueError("该店铺下用户名已存在")
    return users.add(
        User(shop_id=shop_id, username=username, hashed_password=hash_password(password))
    )


def authenticate_user(users: UserRepository, shop_id: str, username: str, password: str) -> User:
    user = users.get_by_username(username, shop_id)
    if not user or not verify_password(password, user.hashed_password):
        raise ValueError("店铺、用户名或密码错误")
    return user


def get_user_by_id(users: UserRepository, user_id: int, shop_id: str) -> User:
    user = users.get_by_id(user_id, shop_id)
    if not user:
        raise ValueError("用户不存在")
    return user
