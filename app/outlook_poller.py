"""Newsletter ingest via the locally running Outlook desktop app (v0.3.0).

Replaces the v0.2.0 IMAP poller. Uses COM automation (``pywin32``) to
talk to whatever Outlook process the user already has open and logged
in — no separate password, no OAuth, no IT ticket. This is the only
practical way to reach a corporate Exchange mailbox where App Passwords
have been disabled by tenant policy.

Design contract:

- ``OUTLOOK_ENABLED`` env flag AND non-empty ``OUTLOOK_FOLDER_PATH``
  are required. Leaving either off makes :func:`outlook_enabled` return
  False; the collector then skips the whole step.
- **Exactly one folder** is read — never the whole mailbox. There is no
  default fallback and no "trust everything" mode: an empty folder path
  is treated as disabled. This is enforced at both the wizard layer
  (400 on empty path) and the runtime layer (:func:`outlook_enabled`).
- ``pywin32`` imports are lazy so this module can be imported on
  non-Windows CI without exploding. COM initialisation happens inside
  :func:`_poll_sync` because COM apartments are per-thread.
- Failures (Outlook not running, folder not found, trust-popup blocked)
  are logged and re-raised; the caller in :mod:`app.collector` wraps
  the call in try/except and turns any failure into a
  :func:`database.record_source_run` entry with success=False, so the
  rest of the pipeline keeps running.
- We NEVER delete or move messages; ``.Read`` is not toggled on the
  ``MailItem``. The tool is strictly read-only against the mailbox.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo  # stdlib on Python 3.9+
except ImportError:  # pragma: no cover - stdlib on all supported platforms
    ZoneInfo = None  # type: ignore

from . import database, url_canon

logger = logging.getLogger(__name__)


def _ensure_pywin32_importable() -> None:
    """v0.4.1 — Belt-and-braces: guarantee ``import pywintypes`` / ``pythoncom``
    work when a caller reaches this module without going through
    :mod:`scripts.launch` first.

    ``launch.py._ensure_pywin32_dlls`` is the primary fix — it runs before any
    ``app`` import when the tool starts through the packaged ``start.bat``.
    But test-connection from the wizard, an admin task, or a hypothetical
    future entry point could import ``outlook_poller`` on a fresh extract
    where DLLs and .pth handling haven't been prepared. This mirrors the
    logic in ``launch.py`` so both paths are safe.

    Idempotent, silently no-ops on non-Windows or dev trees.
    """
    import sys as _sys
    from pathlib import Path as _Path

    if _sys.platform != "win32":
        return
    here = _Path(__file__).resolve().parent  # .../app
    root = here.parent  # extract root
    if not (root / "python" / "python.exe").exists():
        return  # dev tree — trust the venv's pywin32 install
    lib_dir = root / "lib"
    if not lib_dir.is_dir():
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
    for extra in ("win32/lib", "win32"):
        folder = lib_dir / extra
        if folder.is_dir() and str(folder) not in _sys.path:
            _sys.path.insert(0, str(folder))


_ensure_pywin32_importable()


def _local_tz():
    """Best-effort local timezone for interpreting Outlook ``ReceivedTime``.

    Outlook's COM ``ReceivedTime`` is a naive local wall-clock datetime; to
    stamp articles with a correct UTC timestamp we need to know which zone
    that wall-clock is in. Priority:
      1. ``FT_TIMEZONE`` env override (set by app.config from settings).
      2. ``TIMEZONE`` env (same setting under its .env name).
      3. System local tz via ``datetime.now().astimezone().tzinfo``.
      4. ``timezone.utc`` as an absolute fallback (matches pre-v0.3.1
         behaviour so nothing breaks catastrophically if ZoneInfo is
         missing on Windows without tzdata).
    """
    for env_key in ("FT_TIMEZONE", "TIMEZONE"):
        tz_name = (os.environ.get(env_key) or "").strip()
        if tz_name and ZoneInfo is not None:
            try:
                return ZoneInfo(tz_name)
            except Exception:  # pragma: no cover - bad tz string
                logger.debug("Ignoring invalid tz %r from %s", tz_name, env_key)
    try:
        return datetime.now().astimezone().tzinfo or timezone.utc
    except Exception:  # pragma: no cover - defensive
        return timezone.utc


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Outlook env %s not int: %r — using default %s", key, raw, default)
        return default


def _env_flag(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "1" if default else "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def outlook_enabled() -> bool:
    """True iff the flag is on AND a non-empty folder path is set.

    An empty ``OUTLOOK_FOLDER_PATH`` is treated as "not configured"
    rather than "read everything" — the tool refuses to guess a default
    folder for safety.
    """
    if not _env_flag("OUTLOOK_ENABLED"):
        return False
    return bool(_env_str("OUTLOOK_FOLDER_PATH"))


@dataclass
class OutlookConfig:
    folder_path: str       # e.g. "Posteingang/BT Verkehr" or "Inbox/Newsletters"
    store_name: str = ""   # empty = the default (primary) mail store
    max_age_days: int = 30
    max_links_per_email: int = 20

    @classmethod
    def from_env(cls) -> "OutlookConfig":
        return cls(
            folder_path=_env_str("OUTLOOK_FOLDER_PATH"),
            store_name=_env_str("OUTLOOK_STORE_NAME"),
            max_age_days=_env_int("OUTLOOK_MAX_AGE_DAYS", 30),
            max_links_per_email=_env_int("OUTLOOK_MAX_LINKS_PER_EMAIL", 20),
        )


# ---------------------------------------------------------------------------
# URL extraction (shared shape with the former IMAP poller)
# ---------------------------------------------------------------------------

# HTML entities like ``&amp;`` between query params survive the Outlook
# body → we run ``html.unescape`` on every href match before handing off
# to :mod:`url_canon`, so ``utm_source`` doesn't turn into
# ``amp;utm_source`` on the tracker-strip stage.
_HREF_RE = re.compile(
    r"""href\s*=\s*["']\s*(https?://[^"'\s>]+)""",
    re.IGNORECASE,
)
_URL_TEXT_RE = re.compile(
    r"""https?://[^\s<>"']+""",
    re.IGNORECASE,
)

# HTML-tag strip regex used by :func:`_extract_snippet_from_body`. Kept
# minimal on purpose — pulling in bs4 for newsletter body cleanup would
# balloon the release ZIP and 99% of newsletters are clean-enough HTML
# that a tag-strip + whitespace-collapse produces readable pre-screen
# text.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _extract_snippet_from_body(
    html_body: str, text_body: str, *, cap_chars: int = 1500,
) -> Optional[str]:
    """Return a compact plain-text preview for pre-screen + AI scoring.

    v0.3.2: previously newsletter rows landed with ``snippet=None`` so the
    keyword pre-screen only ever saw the mail subject. Many real hits
    ship with a generic subject ("Weekly digest") and the useful signal
    only in the body — those articles fell out. This function pulls the
    first ~1500 chars of body text (plain-text preferred, HTML stripped
    as fallback) so ``ai_filter._row_haystack`` sees them.

    Priority:
      1. ``text_body`` if it's long enough (>=100 chars after collapsing
         whitespace) — plain text is cheapest and least error-prone.
      2. ``html_body`` with ``<script>``/``<style>`` blocks removed, all
         other tags stripped, entities unescaped, whitespace collapsed.
      3. ``text_body`` as-is when both above yield nothing useful.

    Returns ``None`` when neither body has any content (rare but happens
    with meeting-request stubs and other non-mail items that the class
    filter should have already discarded).
    """
    def _clean(s: str) -> str:
        return _WHITESPACE_RE.sub(" ", s or "").strip()

    plain = _clean(text_body)
    if len(plain) >= 100:
        return plain[:cap_chars] or None

    if html_body:
        stripped = _HTML_STYLE_RE.sub(" ", html_body)
        stripped = _HTML_TAG_RE.sub(" ", stripped)
        stripped = html.unescape(stripped)
        stripped = _clean(stripped)
        if stripped:
            return stripped[:cap_chars]

    # Fall back to whatever plain text we had, even if short — better a
    # 20-char snippet than None.
    return plain[:cap_chars] or None


def _extract_urls_from_body(html_body: str, text_body: str, *, cap: int) -> List[str]:
    """Extract up to ``cap`` distinct URLs from a message body.

    Preferentially reads the HTML body (Outlook always populates it for
    newsletters); falls back to the plaintext body when HTML is missing.
    De-duplicated first-occurrence-wins.
    """
    urls: List[str] = []
    seen: set = set()

    def _harvest(payload: str, is_html: bool) -> None:
        pattern = _HREF_RE if is_html else _URL_TEXT_RE
        for match in pattern.finditer(payload or ""):
            raw = match.group(1) if is_html else match.group(0)
            if is_html:
                raw = html.unescape(raw)
            raw = raw.strip().rstrip(",.);>]}'\"")
            if len(raw) > 2048:
                continue
            if raw in seen:
                continue
            seen.add(raw)
            urls.append(raw)
            if len(urls) >= cap:
                return

    # Prefer HTML body: newsletters ship both formats, but the HTML body
    # carries the presentation-quality hrefs. Fall back to plain-text
    # only when HTML found nothing (or wasn't present).
    if html_body:
        _harvest(html_body, is_html=True)
        if urls:
            return urls
    if text_body:
        _harvest(text_body, is_html=False)
    return urls


# ---------------------------------------------------------------------------
# COM folder navigation
# ---------------------------------------------------------------------------


def _list_subfolder_names(folder) -> List[str]:
    """Return the immediate sub-folder names of a COM Folder object.
    Only used to build a helpful error message when the operator's
    ``folder_path`` doesn't resolve."""
    try:
        return [str(f.Name) for f in folder.Folders]
    except Exception:  # pragma: no cover - defensive
        return []


def _resolve_folder(namespace, cfg: OutlookConfig):
    """Navigate ``cfg.folder_path`` under the chosen store and return the
    Folder COM object.

    Raises :class:`LookupError` with an actionable message (including a
    list of the parent's children) when a segment doesn't resolve.
    """
    segments = [s.strip() for s in cfg.folder_path.split("/") if s.strip()]
    if not segments:
        raise LookupError(
            "OUTLOOK_FOLDER_PATH is empty. Set a folder like "
            "'Posteingang/BT Verkehr' before enabling."
        )

    # Pick the store root. Named store first; otherwise the default MAPI store.
    root_folder = None
    if cfg.store_name:
        try:
            # Namespace.Folders is a 1-indexed collection keyed by name.
            root_folder = namespace.Folders[cfg.store_name]
        except Exception:
            store_names = _list_subfolder_names(namespace)
            raise LookupError(
                f"Outlook store {cfg.store_name!r} not found. "
                f"Available stores: {', '.join(store_names) or '(none)'}"
            )
    else:
        # The first segment is expected to be the root folder within the
        # default store, e.g. "Posteingang" or "Inbox". This lets the user
        # spell out the entire path exactly as they see it in Outlook.
        try:
            default_store = namespace.Session.DefaultStore
            root_folder = default_store.GetRootFolder()
        except Exception as exc:
            raise LookupError(f"Cannot access default Outlook store: {exc}")

    # Walk the segments.
    current = root_folder
    for i, seg in enumerate(segments):
        try:
            current = current.Folders[seg]
        except Exception:
            existing = _list_subfolder_names(current)
            hint = ", ".join(existing[:10]) or "(no subfolders)"
            trail = "/".join(segments[:i]) or "(root)"
            raise LookupError(
                f"Outlook folder {seg!r} not found under {trail!r}. "
                f"Available siblings there: {hint}"
            )
    return current


# ---------------------------------------------------------------------------
# Poll loop (sync — wrapped in asyncio.to_thread by caller)
# ---------------------------------------------------------------------------


def _poll_sync(cfg: OutlookConfig) -> Tuple[int, int, int]:
    """Sync Outlook COM poll. Returns (new_articles, duplicates, msgs_processed).

    Never talks to the DB via commit until the article is fully assembled;
    per-message failures are swallowed via inner try/except.
    """
    # Lazy imports so the module can be imported on non-Windows (tests) —
    # the runtime code path is Windows-only.
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is not available — Outlook COM integration requires "
            "Windows + pywin32. Rebuild the release ZIP on Windows to "
            "bundle the wheel."
        ) from exc

    new_articles = 0
    duplicates = 0
    msgs_processed = 0

    # Every COM-consuming thread must init/uninit its apartment.
    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
        except Exception as exc:
            raise RuntimeError(
                f"Cannot open Outlook via COM: {exc}. "
                "Is the Outlook desktop app installed and running? "
                "If a security popup appeared, click "
                "'Allow for 10 minutes'."
            ) from exc

        folder = _resolve_folder(namespace, cfg)

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, cfg.max_age_days))
        # ReceivedTime from COM is naive **local** wall-clock (not UTC).
        # We resolve the local tz once and convert each ReceivedTime to
        # UTC below, so dashboard grouping (which uses ``.astimezone``)
        # gets the correct day.
        local_tz = _local_tz()

        items = folder.Items
        sorted_ok = False
        try:
            items.Sort("[ReceivedTime]", True)  # newest first
            sorted_ok = True
        except Exception as exc:  # pragma: no cover - defensive
            # If the sort fails (locale-specific property names, MAPI
            # restriction errors), items iterate in insertion order —
            # typically oldest-first. In that case the ``break`` below
            # would exit at the first old item and silently skip newer
            # ones. Fall back to ``continue`` and take the O(N) cost of
            # scanning the full folder.
            logger.warning(
                "Outlook: items.Sort failed (%s) — scanning folder without sort",
                exc,
            )

        # We iterate the whole Items collection because COM Restrict on
        # datetimes is fragile across locales. max_age is enforced in the
        # per-item filter below.
        for item in items:
            try:
                # Only ordinary mail items — appointments, meeting requests,
                # calendar items etc. don't have HTMLBody in the shape we
                # want. Class 43 = olMail.
                item_class = getattr(item, "Class", None)
                if item_class is not None and int(item_class) != 43:
                    continue

                received = getattr(item, "ReceivedTime", None)
                if received is None:
                    continue
                # ReceivedTime is a naive local wall-clock datetime — stamp
                # it with the local tz, then convert to UTC for storage.
                try:
                    received_local = datetime(
                        received.year, received.month, received.day,
                        received.hour, received.minute, received.second,
                        tzinfo=local_tz,
                    )
                    received_dt = received_local.astimezone(timezone.utc)
                except Exception:
                    continue
                if received_dt < cutoff:
                    if sorted_ok:
                        # Sorted desc — everything past this point is older.
                        break
                    # Un-sorted iteration: don't stop, just skip this one.
                    continue

                msgs_processed += 1

                subject = str(getattr(item, "Subject", "") or "") or "(newsletter link)"
                sender = str(getattr(item, "SenderEmailAddress", "") or "").strip().lower() or "outlook"

                html_body = str(getattr(item, "HTMLBody", "") or "")
                text_body = str(getattr(item, "Body", "") or "")
                urls = _extract_urls_from_body(
                    html_body, text_body, cap=cfg.max_links_per_email,
                )

                # v0.3.2: pre-compute the body snippet + COM back-reference
                # once per mail — every extracted URL from this mail shares
                # them, so the dashboard can re-open the same message from
                # any of its links.
                body_snippet = _extract_snippet_from_body(
                    html_body, text_body, cap_chars=1500,
                )
                entry_id = str(getattr(item, "EntryID", "") or "") or None
                store_id: Optional[str] = None
                try:
                    parent_store = item.Parent.Store  # noqa: E501 — COM chain
                    store_id = str(getattr(parent_store, "StoreID", "") or "") or None
                except Exception:  # pragma: no cover - defensive
                    # Exotic Outlook store setups may not expose Parent.Store.
                    # We can still round-trip via EntryID alone in most cases.
                    pass

                for raw_url in urls:
                    try:
                        canon = url_canon.canonicalise_url(raw_url, follow_redirects=False)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug("canonicalise failed for %s: %s", raw_url, exc)
                        canon = raw_url
                    if not canon.startswith(("http://", "https://")):
                        continue
                    # Note: country=None means market-scoped negative
                    # keywords never fire on newsletter rows — only globally
                    # scoped negkw apply. Documented asymmetry inherited
                    # from v0.2.0 F3.
                    article_id = database.insert_article(
                        url=canon,
                        title=subject,
                        source_name=sender,
                        source_type="newsletter",
                        country=None,
                        published_at=received_dt,
                        snippet=body_snippet,
                        language=None,
                        outlook_entry_id=entry_id,
                        outlook_store_id=store_id,
                    )
                    if article_id is None:
                        duplicates += 1
                    else:
                        new_articles += 1
            except Exception as exc:
                logger.warning("Outlook: skipping mail item: %s", exc)
                continue
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # pragma: no cover - defensive
            pass

    return new_articles, duplicates, msgs_processed


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------


async def poll(cfg: Optional[OutlookConfig] = None) -> Tuple[int, int, int]:
    """Async entry point used by the collector.

    Runs the sync COM session on a worker thread so the pipeline event
    loop can keep serving /api/stats etc. while this is in flight.
    """
    import asyncio

    if cfg is None:
        cfg = OutlookConfig.from_env()

    if not cfg.folder_path:
        logger.warning("Outlook poll skipped — OUTLOOK_FOLDER_PATH is empty.")
        return 0, 0, 0

    return await asyncio.to_thread(_poll_sync, cfg)


# ---------------------------------------------------------------------------
# Test-connection helper (v0.3.1) — used by the setup wizard and Settings
# ---------------------------------------------------------------------------


def _test_connection_sync(cfg: OutlookConfig) -> dict:
    """Try to reach the configured folder and count items. Read-only.

    Returns a JSON-friendly dict shaped identically to
    ``providers.test_connection`` so the same JS badge logic renders
    both. Never inserts, moves, or modifies any mail item.
    """
    t0 = time.monotonic()
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        return {
            "ok": False,
            "error": (
                f"pywin32 is not installed ({exc}). Outlook integration "
                "only works on Windows with pywin32 available."
            ),
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }

    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    f"Cannot open Outlook via COM: {exc}. Is the Outlook "
                    "desktop app installed and running?"
                ),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        try:
            folder = _resolve_folder(namespace, cfg)
        except LookupError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        try:
            count = int(folder.Items.Count)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Cannot read folder item count: {exc}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        return {
            "ok": True,
            "folder_name": str(getattr(folder, "Name", "") or cfg.folder_path),
            "folder_path": cfg.folder_path,
            "item_count": count,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": None,
        }
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # pragma: no cover - defensive
            pass


async def test_connection(cfg: Optional[OutlookConfig] = None) -> dict:
    """Async wrapper for :func:`_test_connection_sync`.

    Callable from a FastAPI handler — dispatches the sync COM code to a
    worker thread so the event loop stays free.
    """
    import asyncio

    if cfg is None:
        cfg = OutlookConfig.from_env()

    if not cfg.folder_path:
        return {
            "ok": False,
            "error": (
                "Folder path is empty. Set a folder like "
                "'Posteingang/BT Verkehr' before testing."
            ),
            "latency_ms": 0,
        }

    return await asyncio.to_thread(_test_connection_sync, cfg)


# ---------------------------------------------------------------------------
# Open-in-Outlook helper (v0.3.2) — deep-link back into the source mail
# ---------------------------------------------------------------------------


def _open_item_sync(*, entry_id: str, store_id: Optional[str]) -> dict:
    """Rehydrate a :class:`MailItem` from its EntryID and pop it open.

    Read-only against the mailbox: ``MailItem.Display`` shows the inspector
    window; no ``.Read`` toggle, no ``.Move``. If Outlook is closed the
    Dispatch call raises and we surface the error.
    """
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        return {"ok": False, "error": f"pywin32 not available: {exc}"}

    pythoncom.CoInitialize()
    try:
        try:
            app_com = win32com.client.Dispatch("Outlook.Application")
            namespace = app_com.GetNamespace("MAPI")
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    f"Cannot open Outlook via COM: {exc}. "
                    "Is the Outlook desktop app running?"
                ),
            }
        try:
            # Namespace.GetItemFromID takes (entry_id, [store_id]). The
            # store_id is optional — passing None doesn't help, but a
            # falsy store_id lets us call the single-arg overload.
            if store_id:
                item = namespace.GetItemFromID(entry_id, store_id)
            else:
                item = namespace.GetItemFromID(entry_id)
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    f"Cannot resolve the mail item ({type(exc).__name__}: {exc}). "
                    "The mail may have been moved to another folder or deleted."
                ),
            }
        try:
            item.Display()  # non-blocking; returns immediately
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Cannot display the mail item: {exc}",
            }
        return {"ok": True}
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # pragma: no cover - defensive
            pass


async def open_item(
    *, entry_id: str, store_id: Optional[str] = None,
) -> dict:
    """Async wrapper — dispatches the sync COM call to a worker thread."""
    import asyncio

    if not entry_id:
        return {
            "ok": False,
            "error": "No Outlook EntryID stored for this article.",
        }
    return await asyncio.to_thread(
        _open_item_sync, entry_id=entry_id, store_id=store_id,
    )
