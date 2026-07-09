"""Startup wrapper for the packaged FT News Monitor.

Called by ``start.bat`` from the release ZIP. Not usually invoked in the dev
tree — Jordan runs ``python -m uvicorn app.main:app`` directly there. Handles
the concerns you'd otherwise have to write into a ``.bat`` file:

- Locate a free TCP port starting at 8766 (colleague may already have a stale
  copy running, or another process may hold the default).
- Ensure DATA_DIR exists and initialise the SQLite schema before uvicorn tries
  to serve requests.
- Rotate ``server.log`` in DATA_DIR (5MB × 3 files) so 6 months of unattended
  runs don't fill up %LOCALAPPDATA%.
- Open the default browser at the bound URL once ``/api/stats`` returns 200
  (i.e. the app is actually serving, not just accepting sockets).
- Print a "See <DATA_DIR>\\server.log" hint on any hard crash so the pause
  in start.bat gives the colleague something actionable.

Ctrl-C is handled natively by uvicorn.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Make the project importable when run as a script from the ZIP.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_pywin32_dlls() -> None:
    """v0.4.1 — Guarantee pywin32 can be imported from a packaged extract.

    Two independent problems have to be solved for a fresh ZIP install:

    1. **DLL discovery.** ``pip install --target lib/`` bypasses the normal
       ``pywin32_postinstall`` step that copies ``pywintypes313.dll`` and
       ``pythoncom313.dll`` next to the interpreter. We add
       ``lib\\pywin32_system32`` to the DLL-search path (via
       ``os.add_dll_directory``) and to ``PATH`` as belt-and-braces.
    2. **Python-module discovery.** pywin32 ships ``pywintypes.py`` under
       ``lib/win32/lib/`` (not directly in ``lib/``). Its ``pywin32.pth``
       file would normally add that folder to ``sys.path`` — but embedded
       Python's ``.pth`` processing is fragile. We inject the two folders
       (``lib/win32/lib`` and ``lib/win32``) explicitly.

    Idempotent, silently no-ops on non-Windows or dev trees.
    """
    if not sys.platform.startswith("win"):
        return
    lib_dir = ROOT / "lib"
    if not lib_dir.is_dir():
        # Dev tree or unusual extract — fall through, the venv's own
        # pywin32 install will handle things.
        return
    dll_dir = lib_dir / "pywin32_system32"
    if dll_dir.is_dir():
        try:
            os.add_dll_directory(str(dll_dir))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
        path_env = os.environ.get("PATH", "")
        if str(dll_dir) not in path_env.split(os.pathsep):
            os.environ["PATH"] = (
                f"{dll_dir}{os.pathsep}{path_env}" if path_env else str(dll_dir)
            )
    # Prepend the pywin32 module-search folders so ``import pywintypes``
    # and ``import pythoncom`` work without relying on .pth processing.
    for extra in ("win32/lib", "win32"):
        folder = lib_dir / extra
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))


_ensure_pywin32_dlls()


def _bypass_proxy_for_loopback() -> None:
    """v0.6.1 — Extend ``NO_PROXY`` so the readiness probe and any wizard
    "Test" buttons don't accidentally route localhost traffic through a
    corporate HTTP proxy.

    Windows GPO often sets ``HTTP_PROXY`` / ``HTTPS_PROXY`` env-vars for
    the user session. Python's ``urllib`` respects them by default, so
    a poll of ``http://127.0.0.1:8766/api/stats`` would go through the
    proxy — where it 502's because the proxy can't reach the caller's
    own loopback. The tool then thinks the server never came up and
    skips the browser open.

    Idempotent: only appends the loopback hosts if they aren't already
    listed in ``NO_PROXY``.
    """
    existing = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    have = {p.strip() for p in existing.split(",") if p.strip()}
    for host in ("127.0.0.1", "localhost"):
        if host not in have:
            have.add(host)
    os.environ["NO_PROXY"] = ",".join(sorted(have))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


_bypass_proxy_for_loopback()

from app import config, database  # noqa: E402


PORT_RANGE_START = 8766
PORT_RANGE_TRIES = 10
LOG_FILENAME = "server.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
# How long we wait for /api/stats to answer 200 before giving up on the
# browser open (server itself keeps running — this only gates the open).
BROWSER_WAIT_SECONDS = 20.0
BROWSER_POLL_INTERVAL = 0.25


def _setup_logging(data_dir: Path) -> Path:
    """Configure a rotating file logger + console logger.

    The file handler is best-effort: if creating it fails (locked file,
    permission-denied DATA_DIR, EACCES on a corporate-locked profile), we log a
    warning to the console and degrade to console-only. Otherwise start.bat's
    "See <log>" error hint would point at a phantom path that never got
    created, which is worse than no hint at all.
    """
    log_path = data_dir / LOG_FILENAME
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(console)

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.INFO)
        root.addHandler(file_handler)
    except OSError as exc:
        logging.getLogger("launch").warning(
            "Could not open rotating log file %s (%s) — degrading to console-only logging.",
            log_path,
            exc,
        )
    return log_path


def _find_free_port(start: int = PORT_RANGE_START, tries: int = PORT_RANGE_TRIES) -> int:
    """Return the first port in ``[start, start+tries)`` we can bind on
    localhost. Uses SO_REUSEADDR-off semantics so a lingering TIME_WAIT socket
    counts as busy (uvicorn would refuse to bind anyway)."""
    last_err: Exception | None = None
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as err:
                last_err = err
                continue
            return port
    raise RuntimeError(
        f"No free port in range {start}-{start + tries - 1} "
        f"(last error: {last_err})"
    )


def _wait_for_ready(url: str, deadline: float) -> bool:
    """Poll /api/stats until it returns 200 or ``deadline`` elapses.

    Returns True on ready, False on timeout. Any HTTP status other than 200
    or a connection error just triggers another retry until the deadline.

    Uses a proxy-bypassing opener even when the caller's environment has
    ``HTTP_PROXY``/``HTTPS_PROXY`` set — corporate proxies can't reach the
    user's own loopback so a proxied request would 502.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(BROWSER_POLL_INTERVAL)
    return False


def _open_browser_when_ready(host: str, port: int) -> None:
    """Background thread target: wait for /api/stats, then open the browser
    at the app root. Failure to open is logged but not fatal — the URL is
    also printed to stdout so the colleague can copy-paste it."""
    log = logging.getLogger("launch.browser")
    ready_url = f"http://{host}:{port}/api/stats"
    open_url = f"http://{host}:{port}/"
    deadline = time.monotonic() + BROWSER_WAIT_SECONDS
    if not _wait_for_ready(ready_url, deadline):
        log.warning(
            "Server did not become ready within %.0fs; skipping browser open. "
            "Open %s manually.",
            BROWSER_WAIT_SECONDS,
            open_url,
        )
        return
    try:
        webbrowser.open(open_url)
        log.info("Opened browser at %s", open_url)
    except Exception:  # pragma: no cover - platform-specific
        log.exception("Failed to open browser; open %s manually.", open_url)


def _run_uvicorn(port: int) -> int:
    """Import uvicorn lazily so a missing dep produces a clean error message
    instead of the caller staring at a bare ModuleNotFoundError at import
    time.

    Returns the exit code (0 on clean shutdown, non-zero on failure).
    """
    log = logging.getLogger("launch")
    try:
        import uvicorn  # noqa: WPS433
    except ImportError:
        log.exception("uvicorn is not installed — the release ZIP is broken.")
        return 1

    from app.main import app  # noqa: WPS433

    log.info("Serving FT News Monitor on http://127.0.0.1:%d/", port)
    config_uv = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_config=None,  # honour our RotatingFileHandler root logger
        access_log=False,  # too chatty for an unattended tray-style tool
    )
    server = uvicorn.Server(config_uv)
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - manual test
        log.info("Received Ctrl-C; shutting down.")
        return 0
    return 0


def _migrate_dev_db_if_needed() -> None:
    """v0.4.0 — one-shot migration of a dev-layout ``data/monitor.db`` into
    the packaged ``%LOCALAPPDATA%\\FTNewsMonitor`` location.

    Only runs when ALL of these are true:

    * We're in the packaged (embedded-Python) layout, detected by
      ``PROJECT_ROOT/python/python.exe`` existing.
    * The target DB path (``config.DB_PATH``) does not exist yet.
    * A non-empty dev-layout DB lives next to the extract (either in the
      extract's own ``data/`` folder or in the parent's ``data/`` folder,
      to cover both "extracted inside the old dev tree" and "extracted
      alongside").

    The source file is copied (not moved), then a marker file documents
    what happened. Idempotent — the target-exists guard prevents a second
    launch from overwriting user edits. Written as a separate function so
    the migration path stays testable without spinning up uvicorn.
    """
    log = logging.getLogger("launch.migrate")
    target = config.DB_PATH
    if target.exists():
        return
    if not (config.PROJECT_ROOT / "python" / "python.exe").exists():
        return
    candidates = [
        config.PROJECT_ROOT / "data" / "monitor.db",
        config.PROJECT_ROOT.parent / "data" / "monitor.db",
    ]
    for candidate in candidates:
        try:
            if not candidate.exists() or candidate.stat().st_size == 0:
                continue
        except OSError:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # v0.6.1 M8: atomic migration. Copy to a temp sibling first,
            # then os.replace() onto the real target. os.replace is atomic
            # within the same filesystem on Windows and POSIX, so a mid-copy
            # hibernate/crash leaves either the source-DB intact (if the
            # copy hadn't finished) or the finished target (if it had) —
            # never a truncated file at target that a subsequent launch
            # would mistake for a valid DB.
            tmp = target.with_name(target.name + ".migrating")
            try:
                shutil.copy2(candidate, tmp)
                os.replace(tmp, target)
            finally:
                # Best-effort cleanup if os.replace threw.
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
        except OSError:
            log.exception("Failed to migrate %s -> %s", candidate, target)
            return
        marker = target.parent / "MIGRATED_FROM_DEV.txt"
        try:
            marker.write_text(
                f"Migrated {candidate} -> {target} on first packaged launch.\n"
                "The source DB was left in place as a manual backup.\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        log.info("Migrated dev-layout DB from %s to %s", candidate, target)
        return


_SINGLE_INSTANCE_MUTEX_NAME = "Local\\FTNewsMonitor-single-instance"
_PORT_FILE_NAME = "port.txt"


def _acquire_single_instance_handle(data_dir: Path):
    """Return an opaque handle if we are the first instance, None if
    another server is already running under the same Windows session.

    Uses a session-local named mutex (``Local\\<name>``) — that's what a
    Windows GUI installer would use for "already running" detection. Two
    logins on the same laptop each get their own mutex; a shared-desktop
    setup doesn't collide. On non-Windows we fall back to a
    ``pipeline.lock``-style file check (defensive; the tool is Windows-
    only in practice)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            CreateMutexW = kernel32.CreateMutexW
            CreateMutexW.argtypes = [
                wintypes.LPVOID,  # lpMutexAttributes
                wintypes.BOOL,    # bInitialOwner
                wintypes.LPCWSTR, # lpName
            ]
            CreateMutexW.restype = wintypes.HANDLE
            ERROR_ALREADY_EXISTS = 183
            handle = CreateMutexW(None, True, _SINGLE_INSTANCE_MUTEX_NAME)
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                # Another launch.py is running; close our reference
                # (the handle itself points at the same mutex).
                if handle:
                    kernel32.CloseHandle(handle)
                return None
            return ("windows", handle, kernel32)
        except Exception:  # pragma: no cover - very defensive
            return "no-lock"
    # POSIX / dev fallback — best effort using a lockfile.
    lock_path = data_dir / "single_instance.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        return ("posix", fd, lock_path)
    except FileExistsError:
        return None
    except OSError:
        return "no-lock"


def _release_single_instance_handle(handle):
    if handle is None or handle == "no-lock":
        return
    kind = handle[0]
    if kind == "windows":
        _, hmutex, kernel32 = handle
        try:
            kernel32.ReleaseMutex(hmutex)
            kernel32.CloseHandle(hmutex)
        except Exception:  # pragma: no cover
            pass
    elif kind == "posix":
        _, fd, path = handle
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_running_port(data_dir: Path) -> Optional[int]:
    """When another instance is running, read the port it published so we
    can open the browser at the running server instead of failing."""
    path = data_dir / _PORT_FILE_NAME
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _write_running_port(data_dir: Path, port: int) -> None:
    """Publish the bound port so a second launch attempt can find us."""
    try:
        (data_dir / _PORT_FILE_NAME).write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    data_dir = config.DATA_DIR
    log_path = _setup_logging(data_dir)
    log = logging.getLogger("launch")
    log.info("DATA_DIR resolved to %s", data_dir)

    # v0.6.1 M7: single-instance guard. If the user double-clicks start.bat
    # while the server is already running, don't spin up a second uvicorn on
    # a different port with the same DB (WAL is single-writer; two servers
    # would race). Instead: read the running port and open the browser at it.
    handle = _acquire_single_instance_handle(data_dir)
    if handle is None:
        existing = _read_running_port(data_dir)
        if existing:
            open_url = f"http://127.0.0.1:{existing}/"
            log.info("Another server is already running; opening %s", open_url)
            print(f"FT News Monitor is already running at {open_url}")
            try:
                webbrowser.open(open_url)
            except Exception:  # pragma: no cover
                pass
        else:
            print(
                "FT News Monitor appears to be already running. "
                "Check the taskbar for its window, or wait a few seconds "
                "and try again."
            )
        return 0

    try:
        # Migrate a dev-layout DB into the packaged location BEFORE init_db
        # would create an empty file there. See _migrate_dev_db_if_needed for
        # the exact preconditions.
        _migrate_dev_db_if_needed()

        try:
            database.init_db()
        except Exception:
            log.exception("Failed to initialise database")
            print(
                f"\nFT News Monitor could not start. See {log_path} for details.",
                file=sys.stderr,
            )
            return 1

        try:
            port = _find_free_port()
        except RuntimeError:
            log.exception("Port hunt failed")
            print(
                f"\nFT News Monitor could not find a free port. "
                f"See {log_path} for details.",
                file=sys.stderr,
            )
            return 1

        _write_running_port(data_dir, port)

        # Kick off the browser-open in a daemon thread — main thread blocks in
        # uvicorn.Server.run() until the user closes the console.
        threading.Thread(
            target=_open_browser_when_ready,
            args=("127.0.0.1", port),
            daemon=True,
        ).start()

        try:
            return _run_uvicorn(port)
        except Exception:
            log.exception("Uvicorn crashed")
            print(
                f"\nFT News Monitor crashed. See {log_path} for details.",
                file=sys.stderr,
            )
            return 1
    finally:
        _release_single_instance_handle(handle)
        # Best-effort port-file cleanup so a next launch doesn't inherit
        # a stale value (the guard-check would fall through to "already
        # running is a lie" without this).
        try:
            (data_dir / _PORT_FILE_NAME).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
