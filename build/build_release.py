"""Build the portable FT News Monitor release ZIP.

Run:
    venv\\Scripts\\python.exe build/build_release.py            # normal build
    venv\\Scripts\\python.exe build/build_release.py --dry-run  # list the payload only

Output: ``dist/FTNewsMonitor-<VERSION>-win64.zip``
Contents (extract root = ``FTNewsMonitor-<VERSION>/``):

    FTNewsMonitor-<VERSION>/
        python/            <- CPython 3.13.5 embeddable + patched python313._pth
        lib/               <- pip-installed dependencies (requirements.txt)
        app/               <- application package
        scripts/           <- launch.py, run_collection.py, ...
        seeds/             <- shipped keyword/feed seeds (dach, italy, ...)
        data/.gitkeep      <- empty; runtime DATA_DIR is %LOCALAPPDATA%\\FTNewsMonitor
        docs/              <- placeholder (T11 populates)
        start.bat
        .env.example
        VERSION

Extends the T1 probe builder (``build_probe.py``): same download + patch
pattern, but installs the full requirements.txt tree into ``lib/`` and copies
the real app payload instead of a stdlib toy.

Idempotent: rebuilds from a clean staging dir each run. Fails loudly if the
final ZIP is <20MB (missing deps) or >120MB (unexpected crud).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PYTHON_VERSION = "3.13.5"  # pinned to match build_probe.py's cache
PYTHON_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
CACHE_DIR = REPO_ROOT / "build" / "_cache"
CACHED_PY_ZIP = CACHE_DIR / f"python-{PYTHON_VERSION}-embed-amd64.zip"
WHEEL_CACHE = CACHE_DIR / "wheels"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
VERSION_FILE = REPO_ROOT / "VERSION"

# Pure-Python packages published only as sdists. Pip's cross-version install
# (``--python-version`` + ``--only-binary=:all:``) refuses sdists, so we
# pre-``pip wheel`` these once into build/_cache/wheels/ and let pip pick them
# up via ``--find-links``.
SDIST_ONLY_PACKAGES = ("langdetect", "sgmllib3k")

# Directories copied wholesale into the release. Empty target dirs get a
# .gitkeep so ZIP layout is stable. ``docs`` ships the QUICKSTART / ADMIN /
# ROLLOUT / FUTURE / PILOT_CHECKLIST markdown so the release is
# self-contained — the extract answers "how do I use this?" without a
# second email attachment.
INCLUDE_DIRS = ("app", "scripts", "seeds", "docs")

# Files copied verbatim to the release root. ``AGENTS.md`` sits at the
# ZIP root by convention so AI assistants (Claude Code, Cowork) find it
# without the user having to point them at a subfolder — it's a
# deterministic setup runbook for the AWS-Bedrock step of the wizard.
INCLUDE_FILES = ("start.bat", "update.bat", ".env.example", "VERSION", "AGENTS.md")

# Substring exclusion filter applied during the recursive copy of INCLUDE_DIRS.
# Matched against POSIX-normalised relative paths so it works cross-platform.
EXCLUDE_SUBSTR = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "/tests/",
    "/venv/",
    "/build/_cache/",
    ".pyc",
    ".pyo",
)

MIN_SIZE_MB = 20  # <20MB → deps missing
MAX_SIZE_MB = 120  # >120MB → crud crept in


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_version() -> str:
    if not VERSION_FILE.exists():
        raise SystemExit(f"VERSION file missing at {VERSION_FILE}")
    return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"


def _release_name(version: str) -> str:
    return f"FTNewsMonitor-{version}"


def _staging_root(version: str) -> Path:
    return DIST_DIR / _release_name(version)


def _download_python(dest: Path) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHED_PY_ZIP.exists():
        print(f"[python] using cached {CACHED_PY_ZIP.name}")
    else:
        print(f"[python] downloading {PYTHON_URL}")
        urllib.request.urlretrieve(PYTHON_URL, CACHED_PY_ZIP)
        print(f"[python] cached to {CACHED_PY_ZIP}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(CACHED_PY_ZIP) as zf:
        zf.extractall(dest)


def _patch_pth(python_dir: Path) -> None:
    """Rewrite ``python313._pth`` so the embedded interpreter finds our
    vendored ``lib/`` (site-packages equivalent) and the app package root.

    The default ``._pth`` disables ``site``, which also disables ``pip``'s
    directory injection — we replicate its effect manually here.
    """
    pth_files = list(python_dir.glob("python*._pth"))
    if not pth_files:
        raise SystemExit(f"No python*._pth file found in {python_dir}")
    pth = pth_files[0]
    # ..  = release root (so ``import app`` works)
    # ..\lib = vendored deps
    # import site  = let site.py process .pth files inside lib\ (installers
    # occasionally drop them; harmless if none present).
    pth.write_text(
        "python313.zip\n"
        ".\n"
        "..\n"
        "..\\lib\n"
        "import site\n",
        encoding="utf-8",
    )
    print(f"[python] patched {pth.name}")


def _ensure_sdist_wheels() -> None:
    """Materialise pure-Python sdists into ``.whl`` files in the wheel cache.

    ``pip install --python-version 3.13 --only-binary=:all:`` refuses sdists
    (would need to invoke ``build`` under the driver's ABI). We pre-build
    them once per environment so the main install can stay binary-only —
    which is what protects us from accidentally shipping cp314 wheels when
    Jordan's 3.14 venv drives the build.
    """
    WHEEL_CACHE.mkdir(parents=True, exist_ok=True)

    def _has_wheel(pkg: str) -> bool:
        prefix = pkg.replace("-", "_").lower() + "-"
        return any(
            p.suffix == ".whl" and p.name.lower().startswith(prefix)
            for p in WHEEL_CACHE.iterdir()
        )

    missing = [p for p in SDIST_ONLY_PACKAGES if not _has_wheel(p)]
    if not missing:
        print(f"[wheels] cached: {', '.join(SDIST_ONLY_PACKAGES)}")
        return
    print(f"[wheels] building: {', '.join(missing)}")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-deps",
            "--wheel-dir", str(WHEEL_CACHE),
            *missing,
        ],
        check=True,
    )


def _pip_install_deps(python_exe: Path, lib_dir: Path) -> None:
    """Install requirements.txt into ``lib/`` for the embedded interpreter.

    Two things worth calling out:

    1. We drive pip from the *host* Python (``sys.executable``) because pip
       is not bundled with the embeddable ZIP. Using ``--target`` avoids
       needing a get-pip bootstrap step.
    2. The build machine's driver may be a different CPython minor (Jordan
       runs 3.14) than the embedded 3.13 target. Without the version-lock
       flags below, pip resolves ``cp314`` wheels, which the embedded 3.13
       refuses to load. ``--python-version`` + ``--only-binary=:all:`` pin
       the resolver to cp313-compatible wheels. See ``_ensure_sdist_wheels``
       for how pure-Python sdists (langdetect, sgmllib3k) squeeze through
       this binary-only constraint.
    """
    lib_dir.mkdir(parents=True, exist_ok=True)
    _ensure_sdist_wheels()
    py_minor = ".".join(PYTHON_VERSION.split(".")[:2])  # "3.13"
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-warn-script-location",
        "--disable-pip-version-check",
        "--no-compile",  # skip .pyc byte-compile — driver Python (3.14) would
                        # emit cp314 bytecode that the embed 3.13 can't load,
                        # padding the ZIP with ~24 MB of dead weight.
        "--target",
        str(lib_dir),
        "--python-version",
        py_minor,
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--only-binary=:all:",
        "--find-links",
        str(WHEEL_CACHE),
        "-r",
        str(REQUIREMENTS),
    ]
    print(f"[pip] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    # Defensive strip: some wheels ship pre-compiled .pyc files inside
    # __pycache__/. Even without --no-compile catching them, they're for the
    # driver's minor and useless to the embed interpreter.
    _strip_bytecode(lib_dir)


def _strip_bytecode(root: Path) -> int:
    """Remove ``__pycache__`` dirs and stray ``.pyc``/``.pyo`` files under
    ``root``. Returns the number of files+dirs removed (for logging)."""
    removed = 0
    for pycache in list(root.rglob("__pycache__")):
        shutil.rmtree(pycache, ignore_errors=True)
        removed += 1
    for stray in list(root.rglob("*.pyc")):
        try:
            stray.unlink()
            removed += 1
        except OSError:
            pass
    for stray in list(root.rglob("*.pyo")):
        try:
            stray.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[pip] stripped {removed} bytecode artefact(s) from {root.name}/")
    return removed


def _normalise_bat_crlf(path: Path) -> None:
    """Rewrite ``path`` with CRLF line endings.

    ``.bat`` files must ship with CRLF so ``cmd.exe`` parses them correctly —
    a bare-LF ``@echo off`` line survives on modern Windows, but multi-line
    constructs (``if errorlevel``, quoted paths, ``pause``) can subtly break.
    Belt-and-braces alongside the ``.gitattributes`` ``eol=crlf`` rule:
    protects fresh clones from users with ``core.autocrlf=input`` or Git
    Bash pipelines that touch the working copy.
    """
    data = path.read_bytes()
    # Normalise to LF-only first (handles existing CRLF, CR, or LF input),
    # then convert LF -> CRLF for the final write.
    unix = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    path.write_bytes(unix.replace(b"\n", b"\r\n"))


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy ``src`` into ``dst`` (creating ``dst``) while honouring
    EXCLUDE_SUBSTR."""
    if not src.exists():
        print(f"[copy] SKIP missing source: {src}")
        return
    for item in src.rglob("*"):
        rel_posix = item.relative_to(REPO_ROOT).as_posix()
        if any(substr.strip("/") in f"/{rel_posix}/" or substr in rel_posix
               for substr in EXCLUDE_SUBSTR):
            continue
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _copy_payload(release_root: Path) -> None:
    for name in INCLUDE_DIRS:
        src = REPO_ROOT / name
        dst = release_root / name
        print(f"[copy] {name}/")
        _copy_tree(src, dst)

    for filename in INCLUDE_FILES:
        src = REPO_ROOT / filename
        if not src.exists():
            print(f"[copy] SKIP missing file: {filename}")
            continue
        dst = release_root / filename
        shutil.copy2(src, dst)
        if dst.suffix.lower() in (".bat", ".cmd"):
            _normalise_bat_crlf(dst)
            print(f"[copy] {filename} (CRLF normalised)")
        else:
            print(f"[copy] {filename}")

    # Empty runtime dir (bundled so first-run doesn't need to mkdir on a
    # possibly-read-only extract). ``docs/`` is now populated via
    # INCLUDE_DIRS with the shipped markdown, so it no longer belongs here.
    for empty in ("data",):
        target = release_root / empty
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").write_text("", encoding="utf-8")


def _make_zip(release_root: Path, version: str) -> Path:
    zip_path = DIST_DIR / f"{_release_name(version)}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()
    print(f"[zip] {zip_path}")
    # shutil.make_archive builds from a directory whose *contents* become the
    # ZIP entries — we want the release_name/ prefix inside the ZIP so the
    # extract yields a single top-level folder, so we archive the parent and
    # rely on it containing only release_root.
    # DIST_DIR may also contain the old probe ZIP; use a scratch parent dir.
    scratch = DIST_DIR / f"_stage-{version}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    scratch_release = scratch / _release_name(version)
    shutil.move(str(release_root), str(scratch_release))
    try:
        archive_base = str(zip_path).removesuffix(".zip")
        shutil.make_archive(archive_base, "zip", scratch)
    finally:
        # Move the release folder back so a second build_release run doesn't
        # have to re-copy everything (though it does anyway; belt+braces).
        shutil.move(str(scratch_release), str(release_root))
        shutil.rmtree(scratch, ignore_errors=True)
    return zip_path


def _sanity_check(zip_path: Path) -> None:
    size_bytes = zip_path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    print(f"[verify] size: {size_bytes} bytes ({size_mb:.1f} MB)")
    if size_mb < MIN_SIZE_MB:
        raise SystemExit(
            f"ZIP too small ({size_mb:.1f} MB < {MIN_SIZE_MB} MB) — deps missing?"
        )
    if size_mb > MAX_SIZE_MB:
        raise SystemExit(
            f"ZIP too large ({size_mb:.1f} MB > {MAX_SIZE_MB} MB) — crud crept in?"
        )
    with zipfile.ZipFile(zip_path) as zf:
        # Normalise all paths to POSIX so the checks are consistent regardless
        # of the platform that produced the ZIP.
        names = [n.replace("\\", "/") for n in zf.namelist()]
    required = [
        "start.bat",
        "update.bat",
        "python/python.exe",
        "scripts/launch.py",
        "app/main.py",
        "app/config.py",
        "VERSION",
        ".env.example",
        "AGENTS.md",
    ]
    missing = [r for r in required if not any(n.endswith(r) for n in names)]
    if missing:
        raise SystemExit(f"ZIP missing required entries: {missing}")
    # Spot-check vendored deps. Match on the /lib/<pkg> segment so the
    # release-name prefix (FTNewsMonitor-<version>/) is agnostic.
    dep_probes = ["/lib/fastapi/", "/lib/uvicorn/", "/lib/apscheduler/", "/lib/truststore"]
    dep_missing = [d for d in dep_probes if not any(d in n for n in names)]
    if dep_missing:
        raise SystemExit(f"ZIP missing vendored deps: {dep_missing}")
    print("[verify] OK")


def _print_dry_run(version: str) -> None:
    """List every file that would be copied, in POSIX form. No side-effects."""
    print(f"Release: {_release_name(version)}")
    print("Python embeddable:", PYTHON_URL)
    print(f"Requirements: {REQUIREMENTS}")
    print("Payload dirs:")
    for name in INCLUDE_DIRS:
        src = REPO_ROOT / name
        if not src.exists():
            print(f"  MISSING: {name}/")
            continue
        for item in sorted(src.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(REPO_ROOT).as_posix()
            if any(substr in rel or substr.strip("/") in f"/{rel}/"
                   for substr in EXCLUDE_SUBSTR):
                continue
            print(f"  {rel}")
    print("Payload files:")
    for filename in INCLUDE_FILES:
        exists = (REPO_ROOT / filename).exists()
        print(f"  {'OK' if exists else 'MISSING'}  {filename}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build(*, version: str) -> Path:
    release_root = _staging_root(version)
    python_dir = release_root / "python"
    lib_dir = release_root / "lib"

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if release_root.exists():
        print(f"[clean] {release_root}")
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True)

    # 1. Embedded interpreter
    _download_python(python_dir)
    _patch_pth(python_dir)

    # 2. pip install deps into lib/
    _pip_install_deps(python_dir / "python.exe", lib_dir)

    # 3. App payload
    _copy_payload(release_root)

    # 4. ZIP up
    zip_path = _make_zip(release_root, version)

    # 5. Sanity check
    _sanity_check(zip_path)

    print(f"\nRelease ZIP ready: {zip_path}")
    return zip_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build FT News Monitor release ZIP.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the payload inventory without downloading or building.",
    )
    args = parser.parse_args(argv)

    version = _read_version()
    if args.dry_run:
        _print_dry_run(version)
        return 0
    build(version=version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
