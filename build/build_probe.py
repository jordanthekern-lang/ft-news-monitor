"""Build a standalone AppLocker probe ZIP.

Run with any Python 3 (no third-party deps):
    python build/build_probe.py

Output: dist/FTNewsMonitor-probe.zip
Contents:
    FTNewsMonitor-probe/
        python/         ← CPython 3.13 Windows embeddable (amd64)
        probe.py        ← stdlib HTTP server + browser open
        start.bat       ← launcher, pauses on error
"""

import shutil
import socket
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PYTHON_VERSION = "3.13.5"  # pinned: verify SHA at https://www.python.org/downloads/
PYTHON_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
PROBE_DIR = DIST_DIR / "probe" / "FTNewsMonitor-probe"
PYTHON_DIR = PROBE_DIR / "python"
CACHE_DIR = REPO_ROOT / "build" / "_cache"
CACHED_ZIP = CACHE_DIR / f"python-{PYTHON_VERSION}-embed-amd64.zip"

ZIP_OUT = DIST_DIR / "FTNewsMonitor-probe.zip"

# ---------------------------------------------------------------------------
# probe.py contents (stdlib-only)
# ---------------------------------------------------------------------------
PROBE_PY = """\
import http.server
import socket
import threading
import webbrowser

HTML = b\"\"\"<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>FT News Monitor Probe</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px;background:#f0f8ff}
h1{color:#2e7d32;font-size:2.5rem}p{font-size:1.1rem;color:#333}</style>
</head>
<body>
<h1>&#10003; Probe works!</h1>
<p>You can close this window &mdash; please tell Jordan it worked.</p>
</body></html>\"\"\"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, fmt, *args):
        pass  # silence access log


def find_free_port(start=8766):
    for port in range(start, start + 100):
        with socket.socket() as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range %d-%d" % (start, start + 99))


port = find_free_port()
server = http.server.HTTPServer(("", port), Handler)
url = f"http://localhost:{port}/"
print(f"Probe running at {url}")
threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
server.serve_forever()
"""

# ---------------------------------------------------------------------------
# start.bat contents
# ---------------------------------------------------------------------------
START_BAT = (
    "@echo off\r\n"
    "cd /d \"%~dp0\"\r\n"
    r"python\python.exe probe.py" + "\r\n"
    "if errorlevel 1 pause\r\n"
)


def download_python(dest: Path) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHED_ZIP.exists():
        print(f"Using cached {CACHED_ZIP.name}")
        shutil.copy(CACHED_ZIP, dest)
        return
    print(f"Downloading {PYTHON_URL} ...")
    urllib.request.urlretrieve(PYTHON_URL, CACHED_ZIP)
    print(f"Cached to {CACHED_ZIP}")
    shutil.copy(CACHED_ZIP, dest)


def build() -> None:
    # Clean previous probe build
    if PROBE_DIR.exists():
        shutil.rmtree(PROBE_DIR)
    PYTHON_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Download + extract embedded Python
    tmp_zip = DIST_DIR / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    download_python(tmp_zip)
    print(f"Extracting Python to {PYTHON_DIR} ...")
    with zipfile.ZipFile(tmp_zip) as zf:
        zf.extractall(PYTHON_DIR)
    tmp_zip.unlink(missing_ok=True)

    # 2. Write probe.py
    (PROBE_DIR / "probe.py").write_text(PROBE_PY, encoding="utf-8")

    # 3. Write start.bat (already CRLF line endings for Windows compatibility)
    (PROBE_DIR / "start.bat").write_bytes(START_BAT.encode("ascii"))

    # 4. Zip the folder
    if ZIP_OUT.exists():
        ZIP_OUT.unlink()
    archive_base = str(ZIP_OUT).removesuffix(".zip")
    shutil.make_archive(archive_base, "zip", DIST_DIR / "probe")
    print(f"\nProbe ZIP ready: {ZIP_OUT}")

    # 5. Verify
    with zipfile.ZipFile(ZIP_OUT) as zf:
        names = zf.namelist()
    has_bat = any("start.bat" in n for n in names)
    has_probe = any("probe.py" in n for n in names)
    has_python = any("python/" in n or "python\\" in n for n in names)
    print(f"  start.bat present: {has_bat}")
    print(f"  probe.py  present: {has_probe}")
    print(f"  python/   present: {has_python}")
    if not (has_bat and has_probe and has_python):
        raise RuntimeError("ZIP verification failed — missing expected entries")
    print("Build successful.")


if __name__ == "__main__":
    build()
