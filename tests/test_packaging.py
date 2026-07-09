"""Packaging-mode plumbing: DATA_DIR + env_path resolution.

These tests do NOT invoke build_release.py (it downloads ~10MB of embeddable
Python and pip-installs the full dep tree — >30s and network-dependent). They
verify the runtime detection logic that switches between dev-tree paths and
the packaged ``%LOCALAPPDATA%\\FTNewsMonitor`` layout.

We test ``_resolve_data_dir()`` and ``env_path()`` directly rather than
reloading the module — the module-level assignment ``DATA_DIR =
_resolve_data_dir()`` is trivial and the resolver holds the real logic. This
also keeps the shared ``isolated_db`` conftest fixture happy (it monkey-patches
``DATA_DIR`` after import; a reload would undo that mid-test).
"""

from __future__ import annotations

from pathlib import Path

from app import config as app_config


def _patch_embed_layout(monkeypatch, *, present: bool) -> None:
    """Make ``PROJECT_ROOT/python/python.exe`` appear (or not) to exist without
    touching the real filesystem. All other Path.exists calls remain real."""
    real_exists = Path.exists

    def fake_exists(self):
        if self.name == "python.exe" and self.parent.name == "python":
            return present
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def test_data_dir_env_override(monkeypatch, tmp_path):
    """FM_DATA_DIR beats every other signal, including the packaged layout."""
    override = tmp_path / "override_data"
    monkeypatch.setenv("FM_DATA_DIR", str(override))
    _patch_embed_layout(monkeypatch, present=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    assert app_config._resolve_data_dir() == override


def test_data_dir_package_default_when_python_exe_present(monkeypatch, tmp_path):
    """With no FM_DATA_DIR and the embed layout on disk, state lives in
    %LOCALAPPDATA%\\FTNewsMonitor."""
    monkeypatch.delenv("FM_DATA_DIR", raising=False)
    fake_appdata = tmp_path / "fake_appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
    _patch_embed_layout(monkeypatch, present=True)

    assert app_config._resolve_data_dir() == fake_appdata / "FTNewsMonitor"


def test_data_dir_dev_default(monkeypatch):
    """No FM_DATA_DIR, no embed layout → PROJECT_ROOT/data (unchanged)."""
    monkeypatch.delenv("FM_DATA_DIR", raising=False)
    _patch_embed_layout(monkeypatch, present=False)

    assert app_config._resolve_data_dir() == app_config.PROJECT_ROOT / "data"


def test_env_path_follows_data_dir_when_fm_data_dir_set(monkeypatch, tmp_path):
    """FM_DATA_DIR override → .env moves next to the DB.

    ``env_path()`` resolves DATA_DIR live, so setting FM_DATA_DIR is enough —
    no need to patch the module-level DATA_DIR attribute (the pre-fix pattern
    of ``monkeypatch.setattr(app_config, 'DATA_DIR', ...)`` was a workaround
    for import-time caching that no longer applies).
    """
    override = tmp_path / "override_data"
    monkeypatch.setenv("FM_DATA_DIR", str(override))

    assert app_config.env_path() == override / ".env"


def test_env_path_stays_at_project_root_in_dev(monkeypatch):
    """Dev tree keeps .env at PROJECT_ROOT — preserves Jordan's dev workflow
    where secrets sit next to requirements.txt."""
    monkeypatch.delenv("FM_DATA_DIR", raising=False)
    _patch_embed_layout(monkeypatch, present=False)

    assert app_config.env_path() == app_config.PROJECT_ROOT / ".env"


def test_env_path_follows_data_dir_when_embedded_layout(monkeypatch, tmp_path):
    """With the embed layout on disk (packaged deploy) .env moves to DATA_DIR
    even without an explicit FM_DATA_DIR override.

    ``env_path()`` resolves DATA_DIR live via ``_resolve_data_dir()`` so
    stubbing the embed layout + LOCALAPPDATA is sufficient — no need to
    patch the module-level DATA_DIR attribute.
    """
    monkeypatch.delenv("FM_DATA_DIR", raising=False)
    fake_appdata = tmp_path / "fake_appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
    _patch_embed_layout(monkeypatch, present=True)

    assert app_config.env_path() == fake_appdata / "FTNewsMonitor" / ".env"
