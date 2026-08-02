from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tables import UserTable
from app.schemas.schemas import TokenResponse, UserCreate, UserLogin, UserResponse
from app.services.auth_service import authenticate_user, create_access_token, hash_password


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> UserTable:
    if db.scalar(select(UserTable).where(UserTable.username == payload.username)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    user = UserTable(
        username=payload.username,
        password_hash=hash_password(payload.password),
        shop_id="shop-us",
        market="US",
        timezone="America/Los_Angeles",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(payload: UserLogin, db: Session = Depends(get_db)) -> TokenResponse:
    user = authenticate_user(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    return TokenResponse(access_token=create_access_token(user.id, user.shop_id))
