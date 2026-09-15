
# flake8: noqa: E402
import dotenv
dotenv.load_dotenv()

from loguru import logger
from sqlmodel import SQLModel
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncGenerator, Optional

from urllib.parse import quote_plus
import os
import threading
from pathlib import Path


DEFAULT_SQLITE_URL = f"sqlite+aiosqlite:///{Path(__file__).resolve().parent.parent / 'tmp' / 'sqlite' / 'local.db'}"


_POOL_METRICS_LOCK = threading.Lock()
_pool_checkout_total = 0
_pool_checkout_peak = 0


def _read_nonnegative_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def get_database_pool_settings() -> dict[str, int | str]:
    """Return PostgreSQL pool settings shared by API and Worker processes."""
    application_name = os.getenv("DB_APPLICATION_NAME", "ai-framework").strip()
    if not application_name:
        application_name = "ai-framework"
    hostname = os.getenv("HOSTNAME", "").strip()
    if hostname:
        application_name = f"{application_name}:{hostname}"

    return {
        "pool_size": _read_nonnegative_int_env("DB_POOL_SIZE", 5, minimum=1),
        "max_overflow": _read_nonnegative_int_env("DB_MAX_OVERFLOW", 10),
        "pool_timeout_seconds": _read_nonnegative_int_env(
            "DB_POOL_TIMEOUT_SECONDS",
            30,
            minimum=1,
        ),
        "application_name": application_name[:63],
    }


def _install_pool_metrics(async_engine: AsyncEngine) -> None:
    @event.listens_for(async_engine.sync_engine, "checkout")
    def _record_pool_checkout(_dbapi_connection, _connection_record, _connection_proxy) -> None:
        global _pool_checkout_total, _pool_checkout_peak
        checked_out = async_engine.sync_engine.pool.checkedout()
        with _POOL_METRICS_LOCK:
            _pool_checkout_total += 1
            _pool_checkout_peak = max(_pool_checkout_peak, checked_out)


def reset_database_pool_metrics() -> None:
    """Reset process-local checkout counters, primarily at a Run boundary."""
    global _pool_checkout_total, _pool_checkout_peak
    with _POOL_METRICS_LOCK:
        _pool_checkout_total = 0
        _pool_checkout_peak = 0


def get_database_pool_metrics() -> dict[str, Any]:
    """Return lightweight pool telemetry without opening a database connection."""
    engine = _engine
    if engine is None or engine.url.get_backend_name() != "postgresql":
        return {}

    pool = engine.sync_engine.pool
    with _POOL_METRICS_LOCK:
        total_checkouts = _pool_checkout_total
        peak_checked_out = _pool_checkout_peak
    return {
        **get_database_pool_settings(),
        "checked_out": pool.checkedout(),
        "checked_in": pool.checkedin(),
        "overflow": pool.overflow(),
        "total_checkouts": total_checkouts,
        "peak_checked_out": peak_checked_out,
    }


def _ensure_sqlite_parent_dir(db_url: str) -> None:
    database = make_url(db_url).database
    if not database or database == ":memory:":
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def get_async_db_engine() -> AsyncEngine:
    # 从环境变量中读取数据库配置
    if not os.path.exists("./localdata"):
        os.makedirs("./localdata")
    db_type = os.getenv("DB_TYPE", "sqlite")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT")

    if db_type == "postgresql":
        assert db_name, "Postgres db_name不能为空。"
        assert db_host, "Postgres db_host不能为空。"
        assert db_user, "Postgres db_user不能为空。"
        assert db_password, "Postgres db_password不能为空。"
        db_port = int(db_port) if db_port else 5432

        encoded_db_user = quote_plus(db_user)
        encoded_db_password = quote_plus(db_password)

        db_url = f"postgresql+asyncpg://{encoded_db_user}:{encoded_db_password}@{db_host}:{db_port}/{db_name}"
        pool_settings = get_database_pool_settings()
        async_engine = create_async_engine(
            db_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=pool_settings["pool_size"],
            max_overflow=pool_settings["max_overflow"],
            pool_timeout=pool_settings["pool_timeout_seconds"],
            pool_use_lifo=True,
            connect_args={
                "server_settings": {
                    "application_name": pool_settings["application_name"],
                },
            },
        )
        _install_pool_metrics(async_engine)
        logger.info(
            "Created PostgreSQL async engine with {}@{}:{}/{}; "
            "pool_size={}, max_overflow={}, pool_timeout={}s, application_name={}",
            db_user,
            db_host,
            db_port,
            db_name,
            pool_settings["pool_size"],
            pool_settings["max_overflow"],
            pool_settings["pool_timeout_seconds"],
            pool_settings["application_name"],
        )

        return async_engine
    elif db_type == "mysql":
        assert db_name, "MySQL db_name不能为空。"
        assert db_host, "MySQL db_host不能为空。"
        assert db_user, "MySQL db_user不能为空。"
        assert db_password, "MySQL db_password不能为空。"
        db_port = int(db_port) if db_port else 3306

        encoded_db_user = quote_plus(db_user)
        encoded_db_password = quote_plus(db_password)

        db_url = f"mysql+aiomysql://{encoded_db_user}:{encoded_db_password}@{db_host}:{db_port}/{db_name}?charset=utf8mb4"
        async_engine = create_async_engine(
            db_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=10,
            max_overflow=20,
            connect_args={
                "charset": "utf8mb4",
                "use_unicode": True,
            },
        )
        logger.info(
            f"created async engine with MySQL {db_user}@{db_host}:{db_port}/{db_name}"
        )

        return async_engine
    else:
        db_url = os.getenv("SQLITE_URL", DEFAULT_SQLITE_URL)
        _ensure_sqlite_parent_dir(db_url)
        logger.info(f"Creating SQLite engine: {db_url}")

        async_engine = create_async_engine(
            db_url,
            echo=False,
            connect_args={
                "check_same_thread": False,
                "timeout": 60,
            },
            pool_pre_ping=True,
        )

        @event.listens_for(async_engine.sync_engine, "connect")
        def _set_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA cache_size=-64000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
            # logger.debug("SQLite PRAGMA applied: WAL, busy_timeout=60s")

        return async_engine


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------
# The engine and session factory are constructed on first use rather than at
# module-import time. This keeps `import db.db_context` (and any module that
# transitively imports it, such as FastAPI routers under `api.*`) free of side
# effects, which makes unit tests, CLI tools, and Alembic significantly easier
# to reason about.
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first access."""
    global _engine
    if _engine is None:
        _engine = get_async_db_engine()
    return _engine


def get_session_factory() -> async_sessionmaker:
    """Return the process-wide async session factory, bound to the lazy engine."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------
def reset_engine_for_test() -> None:
    """Drop any cached engine/session factory so the next call rebuilds them.

    Intended for unit tests that mutate `DB_TYPE`/`SQLITE_URL` and need a fresh
    engine. Not safe for production use.
    """
    global _engine, _session_factory
    _engine = None
    _session_factory = None
    reset_database_pool_metrics()


def set_engine_for_test(engine: AsyncEngine) -> None:
    """Inject a pre-built engine (e.g. an in-memory mock) for tests."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = None  # force rebuild against the injected engine


def set_session_factory_for_test(factory) -> None:
    """Inject a session factory (e.g. one that yields AsyncMock sessions)."""
    global _session_factory
    _session_factory = factory


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------
# Historical callers and tests reference `async_engine` and `AsyncSessionLocal`
# as module-level attributes. Expose them via PEP 562 `__getattr__` so the
# import surface is unchanged while the actual construction stays lazy.
def __getattr__(name):
    if name == "async_engine":
        return get_engine()
    if name == "AsyncSessionLocal":
        return get_session_factory()
    raise AttributeError(f"module 'db.db_context' has no attribute {name!r}")


# Backwards-compatible alias for the historical misspelling.
# Deprecated: use get_async_db_engine() instead. Will be removed in a future release.
get_async_db_angine = get_async_db_engine


async def init_db():
    import backend.attachments.models  # additive attachment tables
    from backend.attachments.tasks import register_tasks
    register_tasks()
    import db.models.llm  # noqa: F401
    import db.models.message  # noqa: F401
    import db.models.thread  # noqa: F401
    import scheduling.agent_registry.models  # noqa: F401
    import scheduling.discussion.models  # noqa: F401
    import skill.package_models  # noqa: F401
    import task_manager.models  # noqa: F401
    import tool.registry.models  # noqa: F401

    engine = get_engine()

    async def initialize_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        from task_manager.models import ensure_task_manager_schema
        await ensure_task_manager_schema(engine)

    if engine.url.get_backend_name() != "postgresql":
        await initialize_schema()
        return

    # API and Worker processes start together. Serialize their idempotent DDL so
    # PostgreSQL does not deadlock on concurrent ALTER TABLE statements.
    async with engine.connect() as lock_conn:
        await lock_conn.execute(text("SELECT pg_advisory_lock(1827000)"))
        try:
            await initialize_schema()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(1827000)"))


@asynccontextmanager
async def create_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that owns a SQLAlchemy AsyncSession lifecycle.

    Behavior:
        - Creates a new Session from the lazy session factory.
        - On normal exit: commits the session.
        - On exception: rolls back and re-raises.
        - Always closes the session.

    This is the single source of truth for session lifecycle management;
    `get_db_session` (FastAPI dependency) and `with_async_db_session`
    (decorator) both delegate to it to avoid duplication.
    """
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency: yields an AsyncSession per request with
    commit/rollback/close managed by `create_db_session`.
    """
    async with create_db_session() as session:
        yield session


def with_async_db_session(func):
    """Decorator that injects a managed AsyncSession as `session` kwarg."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        async with create_db_session() as session:
            kwargs["session"] = session
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Execution error: {e}")
                raise

    return wrapper
