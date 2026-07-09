"""Tests for the v0.4.0 dev-DB → packaged-layout migration in launch.py.

The migration copies a dev-layout ``<repo>/data/monitor.db`` into the
packaged ``%LOCALAPPDATA%\\FTNewsMonitor\\monitor.db`` on the very first
packaged launch. It's guarded by three preconditions (embedded-Python
layout, target-not-exists, source-non-empty) and must be idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import launch


def _make_packaged_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake extract with ``python/python.exe`` to trigger the
    packaged-layout heuristic. Returns (project_root, target_db_path)."""
    project_root = tmp_path / "extract"
    project_root.mkdir()
    (project_root / "python").mkdir()
    (project_root / "python" / "python.exe").write_bytes(b"")
    # DATA_DIR / DB_PATH we'll point the module's config at.
    data_dir = tmp_path / "AppData" / "FTNewsMonitor"
    return project_root, data_dir / "monitor.db"


def _fake_dev_db(path: Path, sentinel_key: str, sentinel_value: str) -> None:
    """Create a tiny SQLite DB with a sentinel row so we can prove the file
    on disk is the one that was copied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO marker VALUES (?, ?)", (sentinel_key, sentinel_value))
        conn.commit()


def _assert_sentinel(db_path: Path, sentinel_key: str, sentinel_value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM marker WHERE key = ?", (sentinel_key,)
        ).fetchone()
    assert row is not None and row[0] == sentinel_value


def test_migration_copies_dev_db_when_target_missing(tmp_path, monkeypatch):
    project_root, target_db = _make_packaged_layout(tmp_path)
    _fake_dev_db(project_root / "data" / "monitor.db", "who", "jordan")

    from app import config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_config, "DATA_DIR", target_db.parent)
    monkeypatch.setattr(app_config, "DB_PATH", target_db)

    launch._migrate_dev_db_if_needed()

    assert target_db.exists()
    _assert_sentinel(target_db, "who", "jordan")
    # Marker file is created next to the target DB.
    assert (target_db.parent / "MIGRATED_FROM_DEV.txt").exists()
    # Source is left in place as a manual backup.
    assert (project_root / "data" / "monitor.db").exists()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Second call is a no-op — target already exists."""
    project_root, target_db = _make_packaged_layout(tmp_path)
    _fake_dev_db(project_root / "data" / "monitor.db", "who", "original")

    from app import config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_config, "DATA_DIR", target_db.parent)
    monkeypatch.setattr(app_config, "DB_PATH", target_db)

    launch._migrate_dev_db_if_needed()
    # Now mutate the target to prove a second call doesn't clobber it.
    with sqlite3.connect(target_db) as conn:
        conn.execute("UPDATE marker SET value = 'edited' WHERE key = 'who'")
        conn.commit()

    launch._migrate_dev_db_if_needed()  # second pass
    _assert_sentinel(target_db, "who", "edited")  # user's edit survives


def test_migration_skipped_when_no_packaged_layout(tmp_path, monkeypatch):
    """No python/python.exe → we're in dev mode → migration must not run."""
    project_root = tmp_path / "extract"
    project_root.mkdir()
    # DO NOT create python/python.exe.
    _fake_dev_db(project_root / "data" / "monitor.db", "who", "jordan")
    target_db = tmp_path / "AppData" / "FTNewsMonitor" / "monitor.db"

    from app import config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_config, "DATA_DIR", target_db.parent)
    monkeypatch.setattr(app_config, "DB_PATH", target_db)

    launch._migrate_dev_db_if_needed()
    assert not target_db.exists()


def test_migration_skipped_when_no_dev_db(tmp_path, monkeypatch):
    """No source DB anywhere → nothing to do, no error."""
    project_root, target_db = _make_packaged_layout(tmp_path)
    # No data/monitor.db created.

    from app import config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_config, "DATA_DIR", target_db.parent)
    monkeypatch.setattr(app_config, "DB_PATH", target_db)

    launch._migrate_dev_db_if_needed()
    assert not target_db.exists()


def test_migration_finds_dev_db_in_parent(tmp_path, monkeypatch):
    """Extract sits *alongside* the old dev tree; source is at
    ``extract/../data/monitor.db``. Also supported."""
    project_root, target_db = _make_packaged_layout(tmp_path)
    _fake_dev_db(project_root.parent / "data" / "monitor.db", "who", "parent")

    from app import config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_config, "DATA_DIR", target_db.parent)
    monkeypatch.setattr(app_config, "DB_PATH", target_db)

    launch._migrate_dev_db_if_needed()
    assert target_db.exists()
    _assert_sentinel(target_db, "who", "parent")


def test_migration_skips_empty_dev_db(tmp_path, monkeypatch):
    """Zero-byte source is not considered a valid DB — skip."""
    project_root, target_db = _make_packaged_layout(tmp_path)
    src = project_root / "data" / "monitor.db"
    src.parent.mkdir()
    src.write_bytes(b"")  # empty file

    from app import config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(app_config, "DATA_DIR", target_db.parent)
    monkeypatch.setattr(app_config, "DB_PATH", target_db)

    launch._migrate_dev_db_if_needed()
    assert not target_db.exists()
