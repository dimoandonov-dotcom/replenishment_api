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

# Регион за анализите и оборотите (varna | sofia | all). Изгледите rg_* в базата филтрират по него.
from contextvars import ContextVar
from contextlib import contextmanager
from sqlalchemy import event, text

REGION: ContextVar[str] = ContextVar("app_region", default="varna")
REGIONS = ("varna", "sofia", "all")


@event.listens_for(engine, "checkout")
def _set_region(dbapi_conn, rec, proxy):
    try:
        cur = dbapi_conn.cursor()
        cur.execute("SELECT set_config('app.region', %s, false)", (REGION.get(),))
        cur.close()
    except Exception:
        pass


@contextmanager
def region(db: Session, rg: str):
    """Временно друг регион за сесията (напр. отчетът за София в Telegram)."""
    rg = rg if rg in REGIONS else "varna"
    tok = REGION.set(rg)
    db.execute(text("SELECT set_config('app.region', :r, false)"), {"r": rg})
    try:
        yield
    finally:
        REGION.reset(tok)
        try:
            db.execute(text("SELECT set_config('app.region', :r, false)"), {"r": REGION.get()})
        except Exception:
            pass


_COND = """(CASE COALESCE(NULLIF(current_setting('app.region', true), ''), 'varna')
                 WHEN 'all' THEN true
                 WHEN 'sofia' THEN st.mistral_db_path = 'sofia'
                 ELSE COALESCE(st.mistral_db_path, '') <> 'sofia' END)"""


def region_store_ids(db: Session) -> set[int]:
    return {r[0] for r in db.execute(text(f"SELECT st.id FROM stores st WHERE {_COND}")).all()}


def ensure_region_views(db: Session):
    cond = """(CASE COALESCE(NULLIF(current_setting('app.region', true), ''), 'varna')
                 WHEN 'all' THEN true
                 WHEN 'sofia' THEN st.mistral_db_path = 'sofia'
                 ELSE COALESCE(st.mistral_db_path, '') <> 'sofia' END)"""
    for t in ("sa_sales", "sa_stock", "to_receipts"):
        db.execute(text(f"CREATE OR REPLACE VIEW rg_{t} AS SELECT x.* FROM {t} x JOIN stores st ON st.id = x.store_id WHERE {cond}"))
    db.commit()


def get_db():
    """FastAPI dependency - дава сесия и я затваря след заявката."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
