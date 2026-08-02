from fastapi import Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories import Repositories


def get_repositories(db: Session = Depends(get_db)) -> Repositories:
    return Repositories.from_session(db)
