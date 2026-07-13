"""Pytest-Konfiguration für das Backend.

Verantwortlichkeiten:
- .env.dev laden (DB-Passwort, Basis-Config)
- TP_DB_NAME auf taskpilot_test setzen (dedizierte Test-DB)
- Test-DB automatisch erstellen (DROP + CREATE + Schema + Seed)
- @pytest.mark.db skippen wenn PostgreSQL nicht erreichbar
"""

import os
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env.dev"

if _ENV_FILE.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE, override=False)

os.environ["TP_ENV"] = "test"
os.environ["TP_DB_NAME"] = "taskpilot_test"
os.environ.setdefault("TP_DB_HOST", "localhost")
os.environ.setdefault("TP_DB_PORT", "5435")
os.environ.setdefault("TP_DB_USER", "taskpilot")
os.environ.setdefault("TP_DB_PASSWORD", "changeme")
os.environ.setdefault("TP_SECRET_KEY", "test-secret-change-in-production")
os.environ.setdefault("TP_OWNER_EMAIL", "test-owner@innosmith.ai")
os.environ.setdefault("TP_OWNER_PASSWORD", "test-owner-pass-2026")
os.environ.setdefault("TP_OWNER_DISPLAY_NAME", "Test Owner")
os.environ.setdefault("TP_DEBUG", "false")

_DB_HOST = os.environ.get("TP_DB_HOST", "localhost")
_DB_PORT = int(os.environ.get("TP_DB_PORT", "5435"))
_DB_USER = os.environ.get("TP_DB_USER", "taskpilot")
_DB_PASSWORD = os.environ.get("TP_DB_PASSWORD", "changeme")
_DB_NAME = "taskpilot_test"

import pytest


def _install_nullpool_engine() -> None:
    """Test-Harness: App-Engine auf ``NullPool`` umstellen.

    ``pytest-asyncio`` fuehrt jeden async-Test in einem eigenen (funktions-scoped)
    Event-Loop aus. Der modulweite Connection-Pool aus ``app.database`` wuerde
    asyncpg-Connections ueber Loop-Grenzen hinweg wiederverwenden -- die dann an
    einen bereits geschlossenen Loop gebunden sind (``got Future attached to a
    different loop`` / ``Event loop is closed``). ``NullPool`` erzeugt pro Checkout
    eine frische Connection im aktuellen Loop und schliesst sie beim Zurueckgeben,
    wodurch kein Loop-uebergreifender Zustand entsteht.

    Betrifft ausschliesslich den Testlauf; der Produktivcode bleibt unveraendert.
    Muss vor dem Import von ``app.main``/Routern laufen (einige Router binden
    ``async_session`` beim Import), deshalb hier auf Modulebene der conftest.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    import app.database as database

    database.engine = create_async_engine(
        database.settings.database_url, poolclass=NullPool
    )
    database.async_session = async_sessionmaker(
        database.engine, class_=AsyncSession, expire_on_commit=False
    )


_install_nullpool_engine()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: Tests die echte externe Dienste aufrufen (LLM, APIs)"
    )
    config.addinivalue_line(
        "markers", "db: Tests die eine echte PostgreSQL-Verbindung brauchen"
    )


def _db_reachable() -> bool:
    """Prüft ob PostgreSQL auf dem konfigurierten Port erreichbar ist."""
    import socket
    try:
        with socket.create_connection((_DB_HOST, _DB_PORT), timeout=2):
            return True
    except OSError:
        return False


async def _setup_test_db():
    """Erstellt die Test-DB neu: DROP → CREATE → Schema → Seed."""
    import asyncpg

    maintenance_dsn = f"postgresql://{_DB_USER}:{_DB_PASSWORD}@{_DB_HOST}:{_DB_PORT}/postgres"
    conn = await asyncpg.connect(maintenance_dsn)
    try:
        await conn.execute(f"""
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = '{_DB_NAME}' AND pid <> pg_backend_pid()
        """)
        await conn.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")
        await conn.execute(f"CREATE DATABASE {_DB_NAME} OWNER {_DB_USER}")
    finally:
        await conn.close()

    test_dsn = f"postgresql://{_DB_USER}:{_DB_PASSWORD}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}"
    conn = await asyncpg.connect(test_dsn)
    try:
        schema_sql = (_PROJECT_ROOT / "db" / "schema.sql").read_text()
        await conn.execute(schema_sql)

        seed_sql = (_PROJECT_ROOT / "db" / "seed-test.sql").read_text()
        await conn.execute(seed_sql)
    finally:
        await conn.close()


_db_available: bool | None = None
_db_setup_done: bool = False


def pytest_collection_modifyitems(config, items):
    global _db_available, _db_setup_done

    has_db_tests = any("db" in item.keywords for item in items)

    if _db_available is None:
        _db_available = _db_reachable()

    if not _db_available:
        skip_db = pytest.mark.skip(
            reason="PostgreSQL nicht erreichbar — @pytest.mark.db übersprungen"
        )
        for item in items:
            if "db" in item.keywords:
                item.add_marker(skip_db)
        return

    if has_db_tests and not _db_setup_done:
        try:
            asyncio.run(_setup_test_db())
            _db_setup_done = True
            logger.info("Test-DB '%s' erfolgreich erstellt", _DB_NAME)
        except Exception as exc:
            logger.warning("Test-DB Setup fehlgeschlagen: %s", exc)
            skip_db = pytest.mark.skip(
                reason=f"Test-DB Setup fehlgeschlagen: {exc}"
            )
            for item in items:
                if "db" in item.keywords:
                    item.add_marker(skip_db)
