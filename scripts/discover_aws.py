"""v0.7.5 — Discover AWS credentials on the laptop and write them into
``.env`` for the FT News Monitor.

Runs as the last phase of ``install.bat`` inside the freshly created
venv. Mirrors the discovery order documented in ``AGENTS.md`` §F.3 so
Claude-Code and non-AI installs converge on the same behaviour.

Design constraints:

* NEVER echo secret values to stdout — only report *where* a match was
  found (profile name, file path). ``install.log`` captures stdout on
  the colleague's laptop, so a leak here means keys sitting in a plain
  log file forever.
* Prefer ``AWS_PROFILE`` over static keys — SSO refresh keeps rotating
  credentials automatically; static keys go stale.
* Verify via STS before writing. A profile that exists in
  ``~/.aws/config`` but hasn't been logged in yet is useless — silently
  writing it into ``.env`` would give the colleague a red "Test AI"
  banner at wizard step 2 with no clue why.
* Merge, don't overwrite. Colleagues may have hand-edited unrelated
  keys (``GDELT_ENABLED``, ``OUTLOOK_*``, ``COLLECTOR_TIME_WINDOW``,
  comments) — ``app.config.write_env_values`` already does the
  key-preserving merge, so we reuse it.

Exit codes:
    0 — credentials found and written
    2 — no credentials found (colleague must contact Jordan)
    3 — credentials found but STS verify failed (misconfig)
    4 — internal error
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import time
from pathlib import Path

# Repo layout: this file lives at scripts/discover_aws.py — add the
# parent to sys.path so ``import app.config`` works when invoked as a
# plain script (``python scripts/discover_aws.py``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import config as app_config  # noqa: E402  (path-setup above)


_DEFAULT_REGION = "eu-west-1"
_SSO_CACHE_MAX_AGE_S = 8 * 3600  # 8h — a stale SSO token needs re-login


def _aws_dir() -> Path:
    """Location of the ``.aws/`` folder — always under ``~/`` on Windows
    (both %USERPROFILE% and %HOMEDRIVE%%HOMEPATH% expand to that)."""
    return Path.home() / ".aws"


def _log(msg: str) -> None:
    """Progress message to stdout. Never contains secret values.
    ASCII-only so Windows ``cmd.exe`` (cp1252) doesn't corrupt the log."""
    print(f"[discover_aws] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Source 1: environment
# ---------------------------------------------------------------------------


def _from_environment() -> dict | None:
    """Read AWS creds/profile from ``os.environ``."""
    if os.environ.get("AWS_PROFILE"):
        return {
            "kind": "profile",
            "source": "environment (AWS_PROFILE)",
            "AWS_PROFILE": os.environ["AWS_PROFILE"],
        }
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        result = {
            "kind": "keys",
            "source": "environment (AWS_ACCESS_KEY_ID)",
            "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
        }
        if os.environ.get("AWS_SESSION_TOKEN"):
            result["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
        return result
    return None


# ---------------------------------------------------------------------------
# Source 2: ~/.aws/config profiles
# ---------------------------------------------------------------------------


def _config_profiles() -> list[dict]:
    """List every profile declared in ``~/.aws/config``.

    Returns a list of dicts:
        {name: "DeveloperFoo", is_sso: True, region: "eu-west-1"}

    ``configparser`` accepts the ``[profile <name>]`` header verbatim —
    we strip the ``profile `` prefix so the returned name matches what
    boto3 expects in ``AWS_PROFILE``.
    """
    path = _aws_dir() / "config"
    if not path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as exc:
        _log(f"warning: could not parse ~/.aws/config: {exc}")
        return []

    profiles: list[dict] = []
    for raw_section in parser.sections():
        # Both ``[profile foo]`` (named) and ``[default]`` are valid.
        if raw_section == "default":
            name = "default"
        elif raw_section.startswith("profile "):
            name = raw_section[len("profile "):].strip()
        else:
            continue  # ignore non-profile sections like [sso-session foo]
        section = parser[raw_section]
        is_sso = any(
            k in section for k in ("sso_start_url", "sso_session", "sso_role_name")
        )
        profiles.append(
            {
                "name": name,
                "is_sso": is_sso,
                "region": section.get("region", _DEFAULT_REGION),
            }
        )
    return profiles


# ---------------------------------------------------------------------------
# Source 3: ~/.aws/credentials static keys
# ---------------------------------------------------------------------------


def _credentials_profiles() -> list[str]:
    """Names of profiles declared in ``~/.aws/credentials``."""
    path = _aws_dir() / "credentials"
    if not path.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return []
    return list(parser.sections())


# ---------------------------------------------------------------------------
# Source 4: SSO cache freshness check
# ---------------------------------------------------------------------------


def _has_fresh_sso_cache() -> bool:
    """True if ``~/.aws/sso/cache/`` contains any JSON file whose mtime is
    within the SSO token lifetime.

    We only need to know *whether* an SSO session is likely still valid —
    we don't parse the file itself. boto3 does the parsing when it
    actually calls ``sts:GetCallerIdentity``. If the file is stale,
    boto3 will raise ``TokenRetrievalError`` and we drop back to another
    source."""
    cache_dir = _aws_dir() / "sso" / "cache"
    if not cache_dir.exists():
        return False
    now = time.time()
    for entry in cache_dir.iterdir():
        if entry.suffix != ".json":
            continue
        try:
            if now - entry.stat().st_mtime <= _SSO_CACHE_MAX_AGE_S:
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# STS verification — the acid test
# ---------------------------------------------------------------------------


def _verify_profile(profile_name: str, region: str) -> bool:
    """Call ``sts:GetCallerIdentity`` under the given profile. Returns
    True iff it succeeds. Never re-raises."""
    try:
        import boto3
    except ImportError:
        _log("boto3 not installed — cannot verify profile")
        return False
    try:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        session.client("sts").get_caller_identity()
        return True
    except Exception as exc:  # noqa: BLE001 — the whole point is not to raise
        _log(f"  profile '{profile_name}' failed STS verify: {type(exc).__name__}")
        return False


def _verify_static_keys(access_key: str, secret_key: str, region: str,
                        session_token: str | None = None) -> bool:
    """STS-verify a set of static keys directly (no profile involved)."""
    try:
        import boto3
    except ImportError:
        return False
    try:
        kwargs = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
        }
        if session_token:
            kwargs["aws_session_token"] = session_token
        session = boto3.Session(**kwargs)
        session.client("sts").get_caller_identity()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"  static keys failed STS verify: {type(exc).__name__}")
        return False


# ---------------------------------------------------------------------------
# Orchestration — find something that both exists AND verifies
# ---------------------------------------------------------------------------


def discover() -> dict | None:
    """Return a dict of ``.env`` keys to write, or None if nothing works.

    Order (mirrors AGENTS.md F.3):
      1. ``AWS_PROFILE`` in env — verify, use if it works.
      2. Every SSO profile in ``~/.aws/config`` — verify, use first hit.
      3. Every static-keys profile in ``~/.aws/credentials`` — verify.
      4. ``AWS_ACCESS_KEY_ID`` env vars — verify.
    """
    # (1) AWS_PROFILE from env
    env_hit = _from_environment()
    if env_hit and env_hit["kind"] == "profile":
        name = env_hit["AWS_PROFILE"]
        _log(f"trying $AWS_PROFILE = {name} ...")
        # Region: match whatever the profile itself declares; env
        # AWS_REGION wins if set.
        region = os.environ.get("AWS_REGION") or _region_for_profile(name)
        if _verify_profile(name, region):
            _log(f"  ok - writing AWS_PROFILE={name}, AWS_REGION={region}")
            return {"AWS_PROFILE": name, "AWS_REGION": region}

    # (2) SSO profiles in ~/.aws/config - preferred, they rotate
    if _has_fresh_sso_cache():
        _log("SSO cache looks fresh; probing ~/.aws/config profiles")
    else:
        _log("no fresh SSO cache - skipping SSO profiles (would need re-login)")

    for profile in _config_profiles():
        if not profile["is_sso"]:
            continue
        if not _has_fresh_sso_cache():
            break
        name = profile["name"]
        region = profile["region"]
        _log(f"trying SSO profile '{name}' (region {region}) ...")
        if _verify_profile(name, region):
            _log(f"  ok - writing AWS_PROFILE={name}, AWS_REGION={region}")
            return {"AWS_PROFILE": name, "AWS_REGION": region}

    # (3) Non-SSO profiles in ~/.aws/config (static keys via profile)
    for profile in _config_profiles():
        if profile["is_sso"]:
            continue
        name = profile["name"]
        region = profile["region"]
        _log(f"trying static profile '{name}' (region {region}) ...")
        if _verify_profile(name, region):
            _log(f"  ok - writing AWS_PROFILE={name}, AWS_REGION={region}")
            return {"AWS_PROFILE": name, "AWS_REGION": region}

    # (4) Profiles in ~/.aws/credentials that aren't already in config
    seen_names = {p["name"] for p in _config_profiles()}
    for name in _credentials_profiles():
        if name in seen_names:
            continue  # already tried above
        _log(f"trying credentials-only profile '{name}' ...")
        if _verify_profile(name, _DEFAULT_REGION):
            _log(f"  ok - writing AWS_PROFILE={name}, AWS_REGION={_DEFAULT_REGION}")
            return {"AWS_PROFILE": name, "AWS_REGION": _DEFAULT_REGION}

    # (5) AWS_ACCESS_KEY_ID from env - last resort, no rotation
    if env_hit and env_hit["kind"] == "keys":
        _log("trying $AWS_ACCESS_KEY_ID from environment ...")
        region = os.environ.get("AWS_REGION", _DEFAULT_REGION)
        ok = _verify_static_keys(
            env_hit["AWS_ACCESS_KEY_ID"],
            env_hit["AWS_SECRET_ACCESS_KEY"],
            region,
            session_token=env_hit.get("AWS_SESSION_TOKEN"),
        )
        if ok:
            _log("  ok - writing static keys from environment")
            out = {
                "AWS_ACCESS_KEY_ID": env_hit["AWS_ACCESS_KEY_ID"],
                "AWS_SECRET_ACCESS_KEY": env_hit["AWS_SECRET_ACCESS_KEY"],
                "AWS_REGION": region,
            }
            if env_hit.get("AWS_SESSION_TOKEN"):
                out["AWS_SESSION_TOKEN"] = env_hit["AWS_SESSION_TOKEN"]
            return out

    return None


def _region_for_profile(profile_name: str) -> str:
    """Look up a profile's declared region; fall back to eu-west-1."""
    for p in _config_profiles():
        if p["name"] == profile_name:
            return p["region"]
    return _DEFAULT_REGION


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _existing_ai_provider(env_path_override: Path | None = None) -> str:
    """Return the currently configured AI_PROVIDER value (env-file first,
    process env as fallback). Empty string means "not set".

    File-first so a test with ``--env-path <tmp>`` isn't shadowed by the
    developer shell's ``AI_PROVIDER`` value.
    """
    env_file = env_path_override or app_config.env_path()
    if env_file.exists():
        try:
            for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, _, value = stripped.partition("=")
                if name.strip() == "AI_PROVIDER":
                    v = value.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    return v.lower()
        except OSError:
            pass
    return os.environ.get("AI_PROVIDER", "").strip().lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write the discovered credentials into .env (default: dry run)",
    )
    parser.add_argument(
        "--env-path",
        type=Path,
        default=None,
        help="Override the .env target path (default: app.config.env_path())",
    )
    args = parser.parse_args(argv)

    # v0.7.6 — if the colleague picked Cowork mode, the tool doesn't need
    # AWS at all. Skip discovery entirely so a laptop without AWS SSO
    # still finishes install.bat successfully.
    existing_provider = _existing_ai_provider(args.env_path)
    if existing_provider == "cowork":
        _log("AI_PROVIDER=cowork detected - skipping AWS discovery.")
        return 0

    try:
        found = discover()
    except Exception as exc:  # noqa: BLE001
        _log(f"internal error: {type(exc).__name__}: {exc}")
        return 4

    if not found:
        _log("")
        _log("No working AWS credentials found on this laptop.")
        _log("Please contact Jordan — he will send you an Access Key")
        _log("via a secure channel (password manager, not email).")
        return 2

    # Always add AI_PROVIDER=bedrock so the wizard's step-2 default matches.
    found.setdefault("AI_PROVIDER", "bedrock")

    if not args.write_env:
        _log("dry run - pass --write-env to persist")
        _log(f"would write keys: {sorted(found.keys())}")
        return 0

    # Optional override — mainly for tests. Production goes through the
    # app's own resolver so the git-clone layout picks .env at the repo
    # root (not %LOCALAPPDATA%).
    if args.env_path is not None:
        # write_env_values() resolves via env_path() every call, so we
        # monkey-patch its target for the duration of this write.
        orig_env_path = app_config.env_path
        app_config.env_path = lambda p=args.env_path: p  # type: ignore[assignment]
        try:
            app_config.write_env_values(found)
        finally:
            app_config.env_path = orig_env_path  # type: ignore[assignment]
    else:
        app_config.write_env_values(found)

    _log(f".env updated at {args.env_path or app_config.env_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
