#
import os
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, text
try:
    from sqlalchemy.orm import declarative_base
except ImportError:
    from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from passlib.context import CryptContext
import bcrypt
from dotenv import load_dotenv

load_dotenv()

# --- FIX FOR BCRYPT VERSION ERROR ---
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type('about', (object,), {'__version__': bcrypt.__version__})

# --- CONNECT TO CLOUD DATABASE WITH SQLITE FALLBACK ---
DATABASE_URL = os.getenv("DATABASE_URL")

_LOCAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "winvoice.db")
_SQLITE_URL = f"sqlite:///{_LOCAL_DB}"

def _try_cloud(url: str):
    """Try to connect to cloud DB; return engine if successful, None otherwise."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    try:
        eng = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5}
        )
        # Test the connection immediately
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("Connected to Cloud Database (Supabase)")
        return eng
    except Exception as e:
        print(f"[DB] Supabase unavailable ({type(e).__name__}). Falling back to local SQLite.")
        return None

if DATABASE_URL:
    engine = _try_cloud(DATABASE_URL)
    if engine is None:
        engine = create_engine(_SQLITE_URL, connect_args={"check_same_thread": False})
        print(f"[DB] Using local SQLite: {_LOCAL_DB}")
else:
    print("[DB] No DATABASE_URL found. Using local SQLite.")
    engine = create_engine(_SQLITE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    shortcuts = relationship("Shortcut", back_populates="owner")


class Shortcut(Base):
    __tablename__ = "shortcuts"
    id = Column(Integer, primary_key=True, index=True)
    trigger = Column(String)
    action = Column(String)
    owner_email = Column(String, ForeignKey("users.email"))
    owner = relationship("User", back_populates="shortcuts")


def init_db():
    # This creates the tables in the Cloud DB automatically!
    Base.metadata.create_all(bind=engine)


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)