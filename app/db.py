"""Връзка към PostgreSQL. Настройките идват от .env / environment."""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/replenishment",
)

# Railway/Render/Heroku дават URL във формат "postgres://..." или
# "postgresql://..." (без driver) - SQLAlchemy с psycopg2 иска изрично
# "postgresql+psycopg2://...".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency - дава сесия и я затваря след заявката."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
