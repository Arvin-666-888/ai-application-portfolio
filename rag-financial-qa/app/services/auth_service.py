import logging
from datetime import datetime, timedelta

import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import User

logger = logging.getLogger("kb_qa.auth")

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    HAS_PASSLIB = True
except ImportError:
    HAS_PASSLIB = False
    pwd_context = None
    logger.error("passlib is required for password hashing")


def _password_context():
    if not HAS_PASSLIB or pwd_context is None:
        raise RuntimeError("密码哈希依赖不可用")
    return pwd_context


def hash_password(password: str) -> str:
    return _password_context().hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _password_context().verify(plain_password, hashed_password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def register_user(db: Session, username: str, password: str) -> User:
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise ValueError("用户名已存在")

    user = User(
        username=username,
        hashed_password=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"User registered: {username}")
    return user


def authenticate_user(db: Session, username: str, password: str) -> User:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise ValueError("用户名或密码错误")
    if not verify_password(password, user.hashed_password):
        raise ValueError("用户名或密码错误")
    return user


def get_user_by_id(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("用户不存在")
    return user
