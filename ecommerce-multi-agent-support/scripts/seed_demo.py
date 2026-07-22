"""Seed deterministic V1 commerce data."""
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.database import SessionLocal, init_db
from app.services.seed_service import seed_demo_data


def main() -> None:
    init_db()
    with SessionLocal() as db:
        counts = seed_demo_data(db, seed=settings.DEMO_DATA_SEED)
    print(counts)


if __name__ == "__main__":
    main()
