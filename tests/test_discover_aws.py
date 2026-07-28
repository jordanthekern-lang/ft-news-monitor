"""Tests for the v0.7.5 install.bat helper ``scripts/discover_aws.py``.

Covers:
- Environment-based discovery (AWS_PROFILE, static keys)
- ~/.aws/config parsing (SSO vs static profiles)
- ~/.aws/credentials fallback profile enumeration
- SSO cache freshness gate
- STS verify short-circuits (only writes verified profiles)
- Exit codes (0 found, 2 no-creds, 4 internal error)
- .env-merge preserves unrelated keys (regression against future
  refactor breaking the merge contract)

Every test that talks about credentials substitutes a fake STS verify
so no real AWS calls happen.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Add the repo root to sys.path so ``import scripts.discover_aws`` works.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import discover_aws  # noqa: E402


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` to a tmp dir so ~/.aws is isolated.

    Also isolates ``app.config.env_path()`` — otherwise ``main()``
    without ``--env-path`` reads the developer's real .env file, which
    may already carry ``AI_PROVIDER=cowork`` and short-circuit the
    discovery pass.
    """
    monkeypatch.setattr(discover_aws.Path, "home", lambda: tmp_path)
    (tmp_path / ".aws").mkdir()
    fake_env = tmp_path / ".env"
    monkeypatch.setattr(discover_aws.app_config, "env_path", lambda: fake_env)
    # Also clear any AI_PROVIDER from the process env so the file-first
    # lookup falls through to "not set".
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    return tmp_path


def _write_aws_config(home: Path, body: str) -> None:
    (home / ".aws" / "config").write_text(body, encoding="utf-8")


def _write_aws_credentials(home: Path, body: str) -> None:
    (home / ".aws" / "credentials").write_text(body, encoding="utf-8")


def _touch_sso_cache(home: Path, fresh: bool = True) -> None:
    cache = home / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    fake = cache / "abc.json"
    fake.write_text(json.dumps({"accessToken": "x"}), encoding="utf-8")
    if not fresh:
        old = time.time() - (24 * 3600)
        import os as _os
        _os.utime(fake, (old, old))


@pytest.fixture()
def all_verify_ok(monkeypatch):
    """Force every STS verify to succeed. Tests that want to fail
    specific profiles override this."""
    monkeypatch.setattr(discover_aws, "_verify_profile", lambda name, region: True)
    monkeypatch.setattr(
        discover_aws,
        "_verify_static_keys",
        lambda ak, sk, region, session_token=None: True,
    )


@pytest.fixture()
def all_verify_fail(monkeypatch):
    monkeypatch.setattr(discover_aws, "_verify_profile", lambda name, region: False)
    monkeypatch.setattr(
        discover_aws,
        "_verify_static_keys",
        lambda ak, sk, region, session_token=None: False,
    )


# ---------------------------------------------------------------------------
# Source-level unit tests
# ---------------------------------------------------------------------------


def test_from_environment_aws_profile(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "MyProfile")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    hit = discover_aws._from_environment()
    assert hit == {
        "kind": "profile",
        "source": "environment (AWS_PROFILE)",
        "AWS_PROFILE": "MyProfile",
    }


def test_from_environment_static_keys(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    hit = discover_aws._from_environment()
    assert hit["kind"] == "keys"
    assert hit["AWS_ACCESS_KEY_ID"] == "AKIA123"
    assert "AWS_SESSION_TOKEN" not in hit


def test_from_environment_sts_temp_keys(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIA123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-tok")
    hit = discover_aws._from_environment()
    assert hit["AWS_SESSION_TOKEN"] == "session-tok"


def test_from_environment_empty(monkeypatch):
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert discover_aws._from_environment() is None


def test_config_profiles_parses_sso_and_static(fake_home):
    _write_aws_config(
        fake_home,
        """
[profile SsoOne]
sso_start_url = https://flix.awsapps.com/start
sso_role_name = Developer
region = eu-central-1

[profile Static]
region = eu-west-1

[default]
region = us-east-1
""",
    )
    profiles = discover_aws._config_profiles()
    by_name = {p["name"]: p for p in profiles}
    assert by_name["SsoOne"]["is_sso"] is True
    assert by_name["SsoOne"]["region"] == "eu-central-1"
    assert by_name["Static"]["is_sso"] is False
    assert by_name["default"]["region"] == "us-east-1"


def test_config_profiles_missing_file(fake_home):
    # No ~/.aws/config on this fake home yet
    assert discover_aws._config_profiles() == []


def test_credentials_profiles_lists_sections(fake_home):
    _write_aws_credentials(
        fake_home,
        """
[foo]
aws_access_key_id = AKIA1
aws_secret_access_key = secret1

[bar]
aws_access_key_id = AKIA2
aws_secret_access_key = secret2
""",
    )
    assert set(discover_aws._credentials_profiles()) == {"foo", "bar"}


def test_sso_cache_fresh(fake_home):
    _touch_sso_cache(fake_home, fresh=True)
    assert discover_aws._has_fresh_sso_cache() is True


def test_sso_cache_stale(fake_home):
    _touch_sso_cache(fake_home, fresh=False)
    assert discover_aws._has_fresh_sso_cache() is False


def test_sso_cache_missing(fake_home):
    assert discover_aws._has_fresh_sso_cache() is False


# ---------------------------------------------------------------------------
# discover() orchestration
# ---------------------------------------------------------------------------


def test_discover_env_profile_wins(fake_home, monkeypatch, all_verify_ok):
    monkeypatch.setenv("AWS_PROFILE", "FromEnv")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    # A config profile also exists — but env should still win.
    _write_aws_config(fake_home, "[profile Other]\nregion=eu-west-1\n")
    result = discover_aws.discover()
    assert result == {"AWS_PROFILE": "FromEnv", "AWS_REGION": "eu-west-1"}


def test_discover_sso_profile_when_cache_fresh(fake_home, monkeypatch, all_verify_ok):
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(var, raising=False)
    _touch_sso_cache(fake_home, fresh=True)
    _write_aws_config(
        fake_home,
        """
[profile FlixSso]
sso_start_url = https://flix.awsapps.com/start
region = eu-central-1
""",
    )
    result = discover_aws.discover()
    assert result == {"AWS_PROFILE": "FlixSso", "AWS_REGION": "eu-central-1"}


def test_discover_skips_sso_when_cache_stale(fake_home, monkeypatch, all_verify_ok):
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(var, raising=False)
    _touch_sso_cache(fake_home, fresh=False)
    # SSO profile present but cache stale — must move on.
    _write_aws_config(
        fake_home,
        """
[profile FlixSso]
sso_start_url = https://flix.awsapps.com/start
region = eu-central-1

[profile StaticOne]
region = eu-west-1
""",
    )
    result = discover_aws.discover()
    # Falls through to the static profile (which verifies via all_verify_ok).
    assert result == {"AWS_PROFILE": "StaticOne", "AWS_REGION": "eu-west-1"}


def test_discover_returns_none_when_nothing_verifies(fake_home, monkeypatch, all_verify_fail):
    monkeypatch.setenv("AWS_PROFILE", "FromEnv")
    _touch_sso_cache(fake_home, fresh=True)
    _write_aws_config(fake_home, "[profile A]\nsso_start_url=x\n")
    _write_aws_credentials(fake_home, "[B]\naws_access_key_id=x\naws_secret_access_key=y\n")
    assert discover_aws.discover() is None


def test_discover_credentials_only_profile(fake_home, monkeypatch, all_verify_ok):
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(var, raising=False)
    # Only ~/.aws/credentials, no config
    _write_aws_credentials(fake_home, "[cred_only]\naws_access_key_id=x\naws_secret_access_key=y\n")
    result = discover_aws.discover()
    assert result == {"AWS_PROFILE": "cred_only", "AWS_REGION": "eu-west-1"}


def test_discover_env_static_keys_last_resort(fake_home, monkeypatch, all_verify_ok):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    # No config, no credentials file → only path is env static.
    result = discover_aws.discover()
    assert result["AWS_ACCESS_KEY_ID"] == "AKIA1"
    assert result["AWS_REGION"] == "eu-north-1"
    assert "AWS_PROFILE" not in result


# ---------------------------------------------------------------------------
# CLI + .env merge behaviour
# ---------------------------------------------------------------------------


def test_main_exit_code_2_when_nothing_found(fake_home, monkeypatch, all_verify_fail, capsys):
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(var, raising=False)
    rc = discover_aws.main([])
    assert rc == 2


def test_main_exit_code_0_dry_run(fake_home, monkeypatch, all_verify_ok):
    monkeypatch.setenv("AWS_PROFILE", "FromEnv")
    rc = discover_aws.main([])
    assert rc == 0


def test_main_writes_env_and_preserves_unrelated_keys(
    fake_home, monkeypatch, all_verify_ok, tmp_path
):
    """The whole point of using ``app.config.write_env_values``: any
    hand-edited keys (``GDELT_ENABLED``, ``OUTLOOK_*``, comments) survive
    a re-run of the installer."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# hand-added\n"
        "GDELT_ENABLED=true\n"
        "OUTLOOK_FOLDER=Inbox/News\n"
        "AWS_PROFILE=OldProfile\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_PROFILE", "NewProfile")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    rc = discover_aws.main(["--write-env", "--env-path", str(env_file)])
    assert rc == 0

    text = env_file.read_text(encoding="utf-8")
    assert "# hand-added" in text  # comment preserved
    assert "GDELT_ENABLED=true" in text
    assert "OUTLOOK_FOLDER=Inbox/News" in text
    assert "AWS_PROFILE=NewProfile" in text  # replaced in place
    assert "AI_PROVIDER=bedrock" in text  # newly appended default


def test_main_does_not_write_when_dry_run(fake_home, monkeypatch, all_verify_ok, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=1\n", encoding="utf-8")
    monkeypatch.setenv("AWS_PROFILE", "FromEnv")

    rc = discover_aws.main(["--env-path", str(env_file)])
    assert rc == 0
    # File unchanged
    assert env_file.read_text(encoding="utf-8") == "EXISTING=1\n"
