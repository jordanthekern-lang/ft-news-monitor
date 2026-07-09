"""Shared pytest fixtures."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# Make the project importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect DB_PATH and DATA_DIR to a temp dir for every test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "test.db"

    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(app_config, "LOG_PATH", data_dir / "test.log", raising=False)

    # Reload modules that captured the path at import time
    from app import database

    importlib.reload(database)
    database.init_db()

    # Default to "setup already done" so existing tests keep hitting the
    # dashboard / admin routes without being redirected to /setup. Wizard
    # tests reset this by asking for the ``fresh_setup`` fixture.
    from app import runtime_settings

    runtime_settings.set(runtime_settings.SETUP_COMPLETED, True)

    yield db_path


@pytest.fixture(autouse=True)
def _reset_scheduler_singleton():
    """The in-app scheduler is a module-level global — reset it after every
    test so a run that started it (directly or via ``reload()`` on a fresh
    install) doesn't leak a scheduler with a closed event loop into the
    next test."""
    yield
    from app import scheduler as _scheduler

    _scheduler.stop()


@pytest.fixture()
def fresh_setup(isolated_db):
    """Undo the auto-set ``setup_completed`` flag for wizard-gate tests."""
    from app import database as _database

    with _database.get_conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", ("setup_completed",))
    yield isolated_db
