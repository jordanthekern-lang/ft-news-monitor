"""One-shot configurator for Cowork mode.

Run from install.bat when the colleague picks the "C" (Cowork) branch.

What it does:
- Writes ``AI_PROVIDER=cowork`` and a fresh ``COWORK_API_TOKEN`` into
  ``.env`` (via :func:`app.config.write_env_values`, so comments and
  unrelated keys are preserved).
- Drops the same token into ``data/cowork_token.txt`` so the ftnews
  skill can read it without parsing ``.env``.
- Installs a Windows autostart shortcut under
  ``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\``
  that launches ``start.bat`` minimised on login. Without this the
  Cowork skill has no way to bring the tool up — Cowork's Linux VM
  can't spawn Windows processes on the host.

Idempotent: running twice keeps the existing token unless
``--rotate-token`` is passed. Existing shortcut is overwritten.

Exit codes:
    0  success
    1  filesystem / write failure
    2  invalid arguments
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app import config as app_config  # noqa: E402  (needs sys.path tweak)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("configure_cowork")


def _read_env_value(key: str) -> str:
    env_path = app_config.env_path()
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            v = value.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            return v
    return ""


def _write_token_file(token: str) -> Path:
    data_dir = app_config.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = data_dir / "cowork_token.txt"
    token_path.write_text(token + "\n", encoding="utf-8")
    return token_path


def _install_autostart_shortcut(start_bat: Path) -> Path:
    """Drop a .lnk into the user's Startup folder that launches start.bat
    minimised on login.

    Without this the Cowork skill has no way to bring the tool up — the
    Linux VM can't spawn Windows processes on the host. Once the shortcut
    is in place, the tool is always running whenever the user is logged in.

    Uses PowerShell's WScript.Shell COM to author the .lnk. That's the
    standard Windows way and needs no third-party deps.

    Overwrites an existing shortcut so re-running install.bat is safe.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise OSError("APPDATA env var missing — is this Windows?")
    startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)
    shortcut_path = startup_dir / "FT News Monitor.lnk"

    ps_script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{shortcut_path}'); "
        f"$s.TargetPath = '{start_bat}'; "
        f"$s.WorkingDirectory = '{start_bat.parent}'; "
        f"$s.WindowStyle = 7; "  # 7 = minimised, so the terminal doesn't grab focus
        f"$s.Description = 'FT News Monitor server'; "
        f"$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        check=True,
        capture_output=True,
    )
    return shortcut_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="Generate a new COWORK_API_TOKEN even if one already exists.",
    )
    parser.add_argument(
        "--skip-autostart",
        action="store_true",
        help="Don't install the Windows Startup shortcut (dev/tests only).",
    )
    args = parser.parse_args(argv)

    log.info("Configuring Cowork mode.")
    log.info("Target .env: %s", app_config.env_path())

    existing_token = _read_env_value("COWORK_API_TOKEN")
    if args.rotate_token or not existing_token:
        token = secrets.token_hex(32)
        log.info("Generated new COWORK_API_TOKEN.")
    else:
        token = existing_token
        log.info("Kept existing COWORK_API_TOKEN.")

    try:
        app_config.write_env_values(
            {
                "AI_PROVIDER": "cowork",
                "COWORK_API_TOKEN": token,
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to write .env: %s", exc)
        return 1

    try:
        token_path = _write_token_file(token)
    except OSError as exc:
        log.error("Failed to write %s: %s", app_config.DATA_DIR / "cowork_token.txt", exc)
        return 1
    log.info("Cowork token file: %s", token_path)

    if not args.skip_autostart:
        start_bat = REPO_ROOT / "start.bat"
        if not start_bat.exists():
            log.warning(
                "start.bat not found at %s — skipping autostart shortcut.",
                start_bat,
            )
        else:
            try:
                shortcut = _install_autostart_shortcut(start_bat)
                log.info("Installed autostart shortcut: %s", shortcut)
            except (OSError, subprocess.CalledProcessError) as exc:
                # Non-fatal: the user can still start the tool manually.
                log.warning("Could not install autostart shortcut: %s", exc)

    log.info(".env updated. AI_PROVIDER=cowork.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
