"""Tests for the v0.3.0 Outlook-COM newsletter poller.

Strategy: monkey-patch the lazy pywin32 imports inside ``_poll_sync``
with a small in-memory COM stand-in (``_FakeNamespace`` etc.), so tests
run on any platform without an actual Outlook installation.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import database, outlook_poller
from app.main import app


# ---------------------------------------------------------------------------
# Fake COM objects
# ---------------------------------------------------------------------------


class _FakeCOMDateTime:
    """Minimal stand-in for Outlook's ReceivedTime pywintypes.datetime.
    Real objects expose .year/.month/.day/.hour/.minute/.second."""

    def __init__(self, dt: datetime):
        self.year = dt.year
        self.month = dt.month
        self.day = dt.day
        self.hour = dt.hour
        self.minute = dt.minute
        self.second = dt.second


class _FakeParentStore:
    def __init__(self, store_id):
        self.StoreID = store_id


class _FakeParent:
    def __init__(self, store_id):
        self.Store = _FakeParentStore(store_id)


class _FakeMailItem:
    def __init__(self, *, subject, sender, html_body=None, text_body=None,
                 received: datetime, cls=43, entry_id=None, store_id=None):
        self.Class = cls
        self.Subject = subject
        self.SenderEmailAddress = sender
        self.HTMLBody = html_body or ""
        self.Body = text_body or ""
        self.ReceivedTime = _FakeCOMDateTime(received)
        # v0.3.2: EntryID + Parent.Store.StoreID so the poller can persist
        # the COM back-reference. When neither is passed we fall back to
        # sensible-looking hex strings so the resulting rows match the
        # real-Outlook shape without leaking implementation details.
        self.EntryID = entry_id if entry_id is not None else f"ENTRY-{id(self):x}"
        self.Parent = _FakeParent(store_id if store_id is not None else "STORE-abc")


class _FakeItems:
    def __init__(self, items):
        self._items = items

    def Sort(self, key, descending):
        # Sort by ReceivedTime descending — real Outlook sorts in-place.
        self._items.sort(
            key=lambda m: (
                m.ReceivedTime.year, m.ReceivedTime.month, m.ReceivedTime.day,
                m.ReceivedTime.hour, m.ReceivedTime.minute, m.ReceivedTime.second,
            ),
            reverse=bool(descending),
        )

    def __iter__(self):
        return iter(self._items)


class _FakeFolder:
    def __init__(self, name, subfolders=None, items=None):
        self.Name = name
        self._subfolders = {sf.Name: sf for sf in (subfolders or [])}
        self.Items = _FakeItems(items or [])

    # ``folder.Folders["Name"]`` — COM collections are keyed by name.
    @property
    def Folders(self):
        parent_self = self

        class _FolderCollection:
            def __getitem__(self, key):
                if key not in parent_self._subfolders:
                    raise KeyError(key)
                return parent_self._subfolders[key]

            def __iter__(self):
                return iter(parent_self._subfolders.values())

        return _FolderCollection()


class _FakeStore:
    def __init__(self, name, root_folder):
        self.Name = name
        self._root = root_folder

    def GetRootFolder(self):
        return self._root


class _FakeSession:
    def __init__(self, default_store):
        self.DefaultStore = default_store


class _FakeNamespace:
    def __init__(self, stores, default_store):
        self._stores = {s.Name: s for s in stores}
        self._default_store = default_store
        # In real COM, Namespace.Folders yields Folder objects (one per
        # store's top-level container). We approximate by using each
        # store's root folder directly.
        self._folders = [s._root for s in stores]
        self.Session = _FakeSession(default_store)

    @property
    def Folders(self):
        parent_self = self

        class _NsFolders:
            def __getitem__(self, key):
                if key not in parent_self._stores:
                    raise KeyError(key)
                return parent_self._stores[key]._root

            def __iter__(self):
                # Consumed by _list_subfolder_names, which reads .Name — return
                # the store root folders (which each have a Name).
                return iter(parent_self._folders)

        return _NsFolders()


class _FakeOutlookApp:
    def __init__(self, namespace):
        self._namespace = namespace

    def GetNamespace(self, kind):
        assert kind == "MAPI"
        return self._namespace


class _FakePythoncom:
    def CoInitialize(self):
        pass

    def CoUninitialize(self):
        pass


def _install_fake_com(monkeypatch, namespace):
    """Monkey-patch sys.modules with fake pywin32 so ``_poll_sync``'s
    lazy imports pick up the stand-in without touching real Outlook."""
    fake_pythoncom = _FakePythoncom()

    class _FakeDispatch:
        @staticmethod
        def Dispatch(name):
            assert name == "Outlook.Application"
            return _FakeOutlookApp(namespace)

    fake_win32com = types.SimpleNamespace(client=_FakeDispatch())
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", _FakeDispatch())


def _build_ns_with_folder(folder_root: _FakeFolder, store_name="Primary"):
    store = _FakeStore(store_name, folder_root)
    return _FakeNamespace([store], default_store=store)


# ---------------------------------------------------------------------------
# outlook_enabled() env gate
# ---------------------------------------------------------------------------


def test_outlook_disabled_when_flag_off(monkeypatch):
    monkeypatch.delenv("OUTLOOK_ENABLED", raising=False)
    monkeypatch.setenv("OUTLOOK_FOLDER_PATH", "Inbox/Foo")
    assert outlook_poller.outlook_enabled() is False


def test_outlook_disabled_when_folder_path_empty(monkeypatch):
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")
    monkeypatch.setenv("OUTLOOK_FOLDER_PATH", "")
    assert outlook_poller.outlook_enabled() is False


def test_outlook_enabled_when_flag_and_folder_set(monkeypatch):
    monkeypatch.setenv("OUTLOOK_ENABLED", "true")
    monkeypatch.setenv("OUTLOOK_FOLDER_PATH", "Inbox/Foo")
    assert outlook_poller.outlook_enabled() is True


# ---------------------------------------------------------------------------
# OutlookConfig.from_env
# ---------------------------------------------------------------------------


def test_config_from_env_defaults(monkeypatch):
    monkeypatch.setenv("OUTLOOK_FOLDER_PATH", "Inbox/Foo")
    monkeypatch.delenv("OUTLOOK_MAX_AGE_DAYS", raising=False)
    monkeypatch.delenv("OUTLOOK_MAX_LINKS_PER_EMAIL", raising=False)
    monkeypatch.delenv("OUTLOOK_STORE_NAME", raising=False)
    cfg = outlook_poller.OutlookConfig.from_env()
    assert cfg.folder_path == "Inbox/Foo"
    assert cfg.store_name == ""
    assert cfg.max_age_days == 30
    assert cfg.max_links_per_email == 20


def test_config_from_env_junk_int_falls_back(monkeypatch):
    monkeypatch.setenv("OUTLOOK_FOLDER_PATH", "Inbox/Foo")
    monkeypatch.setenv("OUTLOOK_MAX_AGE_DAYS", "notanumber")
    cfg = outlook_poller.OutlookConfig.from_env()
    assert cfg.max_age_days == 30


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------


def test_extract_urls_prefers_html_body_when_present():
    urls = outlook_poller._extract_urls_from_body(
        '<a href="https://a.example/1">a</a><a href="https://b.example/2">b</a>',
        "plain-text should be ignored when html body has content: https://c.example/3",
        cap=10,
    )
    assert urls == ["https://a.example/1", "https://b.example/2"]


def test_extract_urls_falls_back_to_plaintext():
    urls = outlook_poller._extract_urls_from_body(
        "", "See https://example.com/x and https://example.com/y", cap=10,
    )
    assert "https://example.com/x" in urls
    assert "https://example.com/y" in urls


def test_extract_urls_decodes_html_entities():
    """Regression from v0.2.0 F3 fix-loop: &amp; in hrefs must be decoded
    before tracker strip, else utm_source survives as 'amp;utm_source'."""
    urls = outlook_poller._extract_urls_from_body(
        '<a href="https://x.example/p?id=1&amp;utm_source=nl">read</a>',
        "", cap=5,
    )
    assert urls == ["https://x.example/p?id=1&utm_source=nl"]


def test_extract_urls_respects_cap():
    body = "".join(f'<a href="https://x.example/{i}">.</a>' for i in range(50))
    urls = outlook_poller._extract_urls_from_body(body, "", cap=5)
    assert len(urls) == 5


def test_extract_urls_strips_trailing_punctuation():
    urls = outlook_poller._extract_urls_from_body(
        "", "See https://example.com/x, and https://example.com/y.", cap=10,
    )
    assert "https://example.com/x" in urls
    assert "https://example.com/y" in urls


# ---------------------------------------------------------------------------
# Folder resolution
# ---------------------------------------------------------------------------


def test_resolve_folder_happy_path():
    target = _FakeFolder("Newsletters")
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Store Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    got = outlook_poller._resolve_folder(ns, cfg)
    assert got.Name == "Newsletters"


def test_resolve_folder_multi_segment():
    deep = _FakeFolder("Deep")
    mid = _FakeFolder("Newsletters", subfolders=[deep])
    inbox = _FakeFolder("Inbox", subfolders=[mid])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters/Deep")
    got = outlook_poller._resolve_folder(ns, cfg)
    assert got.Name == "Deep"


def test_resolve_folder_missing_segment_lists_siblings():
    a = _FakeFolder("A")
    b = _FakeFolder("B")
    inbox = _FakeFolder("Inbox", subfolders=[a, b])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/NonExistent")
    with pytest.raises(LookupError) as exc:
        outlook_poller._resolve_folder(ns, cfg)
    msg = str(exc.value)
    assert "NonExistent" in msg
    assert "A" in msg and "B" in msg  # sibling hint


def test_resolve_folder_empty_path_raises():
    root = _FakeFolder("Root")
    ns = _build_ns_with_folder(root)
    cfg = outlook_poller.OutlookConfig(folder_path="")
    with pytest.raises(LookupError):
        outlook_poller._resolve_folder(ns, cfg)


def test_resolve_folder_with_store_name():
    inbox = _FakeFolder("Inbox", subfolders=[_FakeFolder("Newsletters")])
    root_a = _FakeFolder("Root A", subfolders=[inbox])
    root_b = _FakeFolder("Root B")
    ns = _FakeNamespace(
        [_FakeStore("Personal", root_a), _FakeStore("Shared", root_b)],
        default_store=_FakeStore("Personal", root_a),
    )
    cfg = outlook_poller.OutlookConfig(
        folder_path="Inbox/Newsletters", store_name="Personal",
    )
    got = outlook_poller._resolve_folder(ns, cfg)
    assert got.Name == "Newsletters"


def test_resolve_folder_unknown_store_lists_available():
    ns = _FakeNamespace(
        [_FakeStore("Personal", _FakeFolder("Root")),
         _FakeStore("Shared", _FakeFolder("Root2"))],
        default_store=_FakeStore("Personal", _FakeFolder("Root")),
    )
    cfg = outlook_poller.OutlookConfig(
        folder_path="Inbox", store_name="Doesnotexist",
    )
    with pytest.raises(LookupError) as exc:
        outlook_poller._resolve_folder(ns, cfg)
    assert "Doesnotexist" in str(exc.value)


# ---------------------------------------------------------------------------
# Full _poll_sync end-to-end with the fake COM tree
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def test_poll_sync_ingests_items(monkeypatch):
    now = _now()
    items = [
        _FakeMailItem(
            subject="Weekly digest",
            sender="news@sustainable-bus.com",
            html_body='<a href="https://sustainable-bus.com/x">x</a>',
            received=now - timedelta(days=1),
        ),
        _FakeMailItem(
            subject="Other digest",
            sender="news@another.example",
            html_body='<a href="https://another.example/y">y</a>',
            received=now - timedelta(days=2),
        ),
    ]
    target = _FakeFolder("Newsletters", items=items)
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)

    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(
        folder_path="Inbox/Newsletters", max_age_days=30,
    )
    new, dup, msgs = outlook_poller._poll_sync(cfg)
    assert new == 2
    assert dup == 0
    assert msgs == 2
    assert database.url_exists("https://sustainable-bus.com/x") is True


def test_poll_sync_skips_older_than_max_age(monkeypatch):
    now = _now()
    old_item = _FakeMailItem(
        subject="Old", sender="s@x.com",
        html_body='<a href="https://x.example/old">old</a>',
        received=now - timedelta(days=90),
    )
    fresh_item = _FakeMailItem(
        subject="Fresh", sender="s@x.com",
        html_body='<a href="https://x.example/fresh">fresh</a>',
        received=now - timedelta(days=2),
    )
    target = _FakeFolder("Newsletters", items=[fresh_item, old_item])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters", max_age_days=7)
    new, dup, msgs = outlook_poller._poll_sync(cfg)
    assert new == 1
    assert msgs == 1
    assert database.url_exists("https://x.example/fresh") is True
    assert database.url_exists("https://x.example/old") is False


def test_poll_sync_dedups_via_url_unique(monkeypatch):
    now = _now()
    items = [
        _FakeMailItem(
            subject="A", sender="s@x.com",
            html_body='<a href="https://x.example/dupe">a</a>',
            received=now - timedelta(hours=2),
        ),
        _FakeMailItem(
            subject="B", sender="s@x.com",
            html_body='<a href="https://x.example/dupe">b</a>',
            received=now - timedelta(hours=1),
        ),
    ]
    target = _FakeFolder("Newsletters", items=items)
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    new, dup, msgs = outlook_poller._poll_sync(cfg)
    assert new == 1
    assert dup == 1
    assert msgs == 2


def test_poll_sync_strips_tracker_params(monkeypatch):
    now = _now()
    items = [
        _FakeMailItem(
            subject="tracked", sender="s@x.com",
            html_body='<a href="https://x.example/read?utm_source=nl&amp;id=42">read</a>',
            received=now - timedelta(days=1),
        ),
    ]
    target = _FakeFolder("Newsletters", items=items)
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    outlook_poller._poll_sync(cfg)
    # utm_source stripped, id preserved. And &amp; decoded to & first.
    assert database.url_exists("https://x.example/read?id=42") is True
    assert database.url_exists("https://x.example/read?utm_source=nl&id=42") is False


def test_poll_sync_skips_non_mail_items(monkeypatch):
    now = _now()
    appointment = _FakeMailItem(
        subject="Cal", sender="s@x.com",
        html_body='<a href="https://cal.example/x">x</a>',
        received=now - timedelta(hours=1),
        cls=26,  # olAppointment
    )
    mail = _FakeMailItem(
        subject="Real", sender="s@x.com",
        html_body='<a href="https://real.example/x">x</a>',
        received=now - timedelta(hours=2),
    )
    target = _FakeFolder("Newsletters", items=[appointment, mail])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    new, dup, msgs = outlook_poller._poll_sync(cfg)
    assert msgs == 1
    assert database.url_exists("https://real.example/x") is True
    assert database.url_exists("https://cal.example/x") is False


def test_poll_sync_per_item_error_isolation(monkeypatch):
    """A broken item must not kill the loop — remaining items still process."""
    now = _now()

    class _BrokenItem:
        Class = 43

        @property
        def ReceivedTime(self):
            raise RuntimeError("intentional test explosion")

    good = _FakeMailItem(
        subject="Good", sender="s@x.com",
        html_body='<a href="https://good.example/x">x</a>',
        received=now - timedelta(hours=1),
    )
    target = _FakeFolder("Newsletters", items=[_BrokenItem(), good])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    new, dup, msgs = outlook_poller._poll_sync(cfg)
    assert new == 1
    assert database.url_exists("https://good.example/x") is True


def test_poll_sync_folder_not_found(monkeypatch):
    inbox = _FakeFolder("Inbox", subfolders=[_FakeFolder("Existing")])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/DoesNotExist")
    with pytest.raises(LookupError):
        outlook_poller._poll_sync(cfg)


def test_poll_async_returns_zero_when_folder_empty(monkeypatch):
    cfg = outlook_poller.OutlookConfig(folder_path="")
    assert asyncio.run(outlook_poller.poll(cfg)) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Wizard route
# ---------------------------------------------------------------------------


def test_wizard_route_writes_env(monkeypatch, fresh_setup, tmp_path):
    from app import config as _config

    env_path = tmp_path / ".env"
    monkeypatch.setattr(_config, "env_path", lambda: env_path, raising=False)

    client = TestClient(app)
    r = client.post(
        "/setup/outlook",
        data={
            "outlook_enabled": "1",
            "outlook_folder_path": "Posteingang/BT Verkehr",
            "outlook_store_name": "",
            "outlook_max_age_days": "45",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    body = env_path.read_text(encoding="utf-8")
    assert "OUTLOOK_ENABLED=true" in body
    assert 'OUTLOOK_FOLDER_PATH="Posteingang/BT Verkehr"' in body \
        or "OUTLOOK_FOLDER_PATH=Posteingang/BT Verkehr" in body
    assert "OUTLOOK_MAX_AGE_DAYS=45" in body


def test_wizard_route_refuses_enable_without_folder(monkeypatch, fresh_setup, tmp_path):
    from app import config as _config

    env_path = tmp_path / ".env"
    monkeypatch.setattr(_config, "env_path", lambda: env_path, raising=False)

    client = TestClient(app)
    r = client.post(
        "/setup/outlook",
        data={
            "outlook_enabled": "1",
            "outlook_folder_path": "",  # required but empty
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_wizard_route_refuses_populated_without_enable(monkeypatch, fresh_setup, tmp_path):
    from app import config as _config

    env_path = tmp_path / ".env"
    monkeypatch.setattr(_config, "env_path", lambda: env_path, raising=False)

    client = TestClient(app)
    r = client.post(
        "/setup/outlook",
        data={
            # NO outlook_enabled
            "outlook_folder_path": "Inbox/Newsletters",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_wizard_route_disable_clears_values(monkeypatch, fresh_setup, tmp_path):
    from app import config as _config

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OUTLOOK_FOLDER_PATH=old/path\n"
        "OUTLOOK_STORE_NAME=old-store\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_config, "env_path", lambda: env_path, raising=False)

    client = TestClient(app)
    r = client.post(
        "/setup/outlook",
        data={},  # blank submit — explicit disable
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    body = env_path.read_text(encoding="utf-8")
    assert "OUTLOOK_ENABLED=false" in body
    assert "OUTLOOK_FOLDER_PATH=old/path" not in body
    assert "OUTLOOK_FOLDER_PATH=" in body  # cleared to empty


# ---------------------------------------------------------------------------
# v0.3.1 — test_connection helper + /setup/test-outlook route
# ---------------------------------------------------------------------------


def test_test_connection_empty_folder_path_returns_error():
    cfg = outlook_poller.OutlookConfig(folder_path="")
    result = asyncio.run(outlook_poller.test_connection(cfg))
    assert result["ok"] is False
    assert "empty" in result["error"].lower()


def test_test_connection_happy_path(monkeypatch):
    items = [
        _FakeMailItem(
            subject="one", sender="s@x.com",
            html_body='<a href="https://x.example/1">1</a>',
            received=_now() - timedelta(days=1),
        ),
        _FakeMailItem(
            subject="two", sender="s@x.com",
            html_body='<a href="https://x.example/2">2</a>',
            received=_now() - timedelta(days=2),
        ),
    ]
    # Attach a .Count that iterates the fake list.
    target = _FakeFolder("Newsletters", items=items)
    target.Items.Count = len(items)  # type: ignore[attr-defined]
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    result = asyncio.run(outlook_poller.test_connection(cfg))
    assert result["ok"] is True
    assert result["folder_name"] == "Newsletters"
    assert result["item_count"] == 2
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


def test_test_connection_folder_not_found_returns_error_string(monkeypatch):
    inbox = _FakeFolder("Inbox", subfolders=[])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/DoesNotExist")
    result = asyncio.run(outlook_poller.test_connection(cfg))
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_wizard_test_outlook_route_uses_form_values(monkeypatch, fresh_setup):
    # Route wires form values into an OutlookConfig, then calls
    # test_connection — so patching test_connection directly is enough to
    # verify the wiring.
    captured = {}

    async def fake_test(cfg):
        captured["folder_path"] = cfg.folder_path
        captured["store_name"] = cfg.store_name
        return {"ok": True, "folder_name": "X", "item_count": 5, "latency_ms": 3, "error": None}

    monkeypatch.setattr(outlook_poller, "test_connection", fake_test)

    client = TestClient(app)
    r = client.post(
        "/setup/test-outlook",
        data={
            "outlook_folder_path": "Inbox/Foo",
            "outlook_store_name": "acc@example.com",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["item_count"] == 5
    assert captured["folder_path"] == "Inbox/Foo"
    assert captured["store_name"] == "acc@example.com"


# ---------------------------------------------------------------------------
# v0.3.1 — items.Sort() silent-failure fix (P4b)
# ---------------------------------------------------------------------------


class _FakeItemsSortFails(_FakeItems):
    """Iterates oldest-first (typical insertion order) and raises on Sort."""

    def Sort(self, key, descending):
        raise RuntimeError("MAPI Sort refused this property")


def test_poll_sync_sort_failure_still_reads_newer_items(monkeypatch):
    """If items.Sort raises, the iteration falls back to insertion order.
    Historically the code then hit a ``break`` at the first old item and
    silently dropped later fresh items. The fix converts that to a
    ``continue`` so old items are skipped but fresh ones are still ingested.
    """
    now = _now()
    old_item = _FakeMailItem(
        subject="old", sender="s@x.com",
        html_body='<a href="https://x.example/old-1">old</a>',
        received=now - timedelta(days=90),
    )
    fresh_item = _FakeMailItem(
        subject="fresh", sender="s@x.com",
        html_body='<a href="https://x.example/fresh-1">fresh</a>',
        received=now - timedelta(days=1),
    )

    # Insertion order: old first, fresh second — the pathological case.
    target = _FakeFolder("Newsletters", items=[old_item, fresh_item])
    target.Items = _FakeItemsSortFails([old_item, fresh_item])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters", max_age_days=7)
    new, dup, msgs = outlook_poller._poll_sync(cfg)
    # Old item skipped (max_age); fresh item ingested.
    assert new == 1
    assert database.url_exists("https://x.example/fresh-1") is True
    assert database.url_exists("https://x.example/old-1") is False


# ---------------------------------------------------------------------------
# v0.3.1 — ReceivedTime interpreted as local wall-clock (P4c)
# ---------------------------------------------------------------------------


def test_receivedtime_treated_as_local_wallclock(monkeypatch):
    """Outlook's ReceivedTime is naive local time. The poller must convert
    it to UTC using the configured local tz so dashboard date grouping
    is correct."""
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")

    # A message received at 23:30 Berlin on 2026-07-06 = 21:30 UTC.
    # Berlin is UTC+2 in July (CEST).
    received_local = datetime(2026, 7, 6, 23, 30, 0)
    item = _FakeMailItem(
        subject="late-night",
        sender="s@x.com",
        html_body='<a href="https://x.example/late">late</a>',
        # Feed the fake with a naive datetime interpreted as local.
        received=received_local,
    )
    target = _FakeFolder("Newsletters", items=[item])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    # Freeze the cutoff far in the past so the item can't get filtered out
    # by the age check just because 2026-07-06 might be > 30 days behind
    # whatever ``datetime.now()`` returns during the test.
    cfg = outlook_poller.OutlookConfig(
        folder_path="Inbox/Newsletters", max_age_days=100_000,
    )
    new, _, _ = outlook_poller._poll_sync(cfg)
    assert new == 1

    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE url = ?", ("https://x.example/late",),
        ).fetchone()
    assert row is not None
    stored = row["published_at"]
    # Parse the ISO string and convert to UTC for comparison.
    ts = datetime.fromisoformat(stored)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    # 23:30 Europe/Berlin in July = 21:30 UTC.
    assert ts.astimezone(timezone.utc).hour == 21
    assert ts.astimezone(timezone.utc).minute == 30


# ---------------------------------------------------------------------------
# v0.3.2 — body-snippet extraction feeds the pre-screen
# ---------------------------------------------------------------------------


def test_extract_snippet_prefers_plain_text():
    plain = "Fördermittel für Elektrobusse werden ausgeweitet." * 5
    html_body = "<html><body>ignored html noise</body></html>"
    snippet = outlook_poller._extract_snippet_from_body(html_body, plain)
    assert snippet is not None
    assert snippet.startswith("Fördermittel")
    assert "html" not in snippet.lower()


def test_extract_snippet_falls_back_to_html_when_plain_is_short():
    plain = "tiny"  # <100 chars → HTML wins
    html_body = (
        "<html><body><p>Berlin plans a new procurement round for "
        "zero-emission coaches in Q4.</p></body></html>"
    )
    snippet = outlook_poller._extract_snippet_from_body(html_body, plain)
    assert snippet is not None
    assert "procurement round" in snippet
    assert "<p>" not in snippet


def test_extract_snippet_strips_scripts_and_styles():
    html_body = (
        "<html><head><style>.junk{color:red}</style>"
        "<script>alert('x')</script></head>"
        "<body><p>Body text keeper.</p></body></html>"
    )
    snippet = outlook_poller._extract_snippet_from_body(html_body, "")
    assert snippet is not None
    assert "Body text keeper" in snippet
    assert "alert" not in snippet
    assert "color:red" not in snippet


def test_extract_snippet_caps_length():
    plain = "x" * 5000
    snippet = outlook_poller._extract_snippet_from_body("", plain, cap_chars=1500)
    assert snippet is not None
    assert len(snippet) == 1500


def test_extract_snippet_returns_none_when_empty():
    assert outlook_poller._extract_snippet_from_body("", "") is None


def test_extract_snippet_unescapes_entities():
    html_body = "<p>Autocarros &amp; ônibus &ndash; programa 2026.</p>"
    snippet = outlook_poller._extract_snippet_from_body(html_body, "")
    assert snippet is not None
    assert "&amp;" not in snippet
    assert "&" in snippet  # unescaped


# ---------------------------------------------------------------------------
# v0.3.2 — poll_sync stores body-snippet + EntryID + StoreID
# ---------------------------------------------------------------------------


def test_poll_sync_persists_body_snippet_and_entry_id(monkeypatch):
    now = _now()
    item = _FakeMailItem(
        subject="Weekly digest",  # generic subject on purpose
        sender="news@sustainable-bus.com",
        html_body=(
            '<html><body>'
            '<p>New funding programme for electric coaches announced by '
            'the German transport ministry today.</p>'
            '<a href="https://sustainable-bus.com/x">read more</a>'
            '</body></html>'
        ),
        text_body="",
        received=now - timedelta(days=1),
        entry_id="ENTRY-0001",
        store_id="STORE-xyz",
    )
    target = _FakeFolder("Newsletters", items=[item])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    new, _, _ = outlook_poller._poll_sync(cfg)
    assert new == 1

    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE url = ?",
            ("https://sustainable-bus.com/x",),
        ).fetchone()
    assert row is not None
    assert row["source_type"] == "newsletter"
    # Body snippet was persisted — no more None.
    assert row["snippet"] is not None
    assert "funding programme" in row["snippet"]
    # COM back-reference stored so /outlook/open can rehydrate.
    assert row["outlook_entry_id"] == "ENTRY-0001"
    assert row["outlook_store_id"] == "STORE-xyz"


def test_poll_sync_shares_snippet_and_entry_across_urls(monkeypatch):
    """When one mail has multiple links, every row from that mail must
    point back to the same EntryID + carry the same body-snippet — so
    clicking Open in Outlook from any of them opens the same email."""
    now = _now()
    item = _FakeMailItem(
        subject="Multi-link digest",
        sender="news@example.com",
        html_body=(
            '<p>Berlin funds new electric coach programme.</p>'
            '<a href="https://example.com/a">A</a>'
            '<a href="https://example.com/b">B</a>'
        ),
        received=now - timedelta(days=1),
        entry_id="ENTRY-SHARED",
        store_id="STORE-shared",
    )
    target = _FakeFolder("Newsletters", items=[item])
    inbox = _FakeFolder("Inbox", subfolders=[target])
    root = _FakeFolder("Root", subfolders=[inbox])
    ns = _build_ns_with_folder(root)
    _install_fake_com(monkeypatch, ns)

    cfg = outlook_poller.OutlookConfig(folder_path="Inbox/Newsletters")
    new, _, _ = outlook_poller._poll_sync(cfg)
    assert new == 2

    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT url, outlook_entry_id, snippet FROM articles WHERE outlook_entry_id = ?",
            ("ENTRY-SHARED",),
        ).fetchall()
    assert len(rows) == 2
    snippets = {r["snippet"] for r in rows}
    assert len(snippets) == 1  # both rows share the mail snippet
    assert "electric coach programme" in list(snippets)[0]


# ---------------------------------------------------------------------------
# v0.3.2 — pre-screen sees body-text tokens (not just the subject)
# ---------------------------------------------------------------------------


def test_prescreen_matches_body_snippet(monkeypatch):
    """Real regression this feature fixes: a newsletter with a generic
    subject but a strong keyword in the body used to fall out of the
    pre-screen because ``snippet`` was NULL. Now the body-snippet is
    persisted and the pre-screen haystack picks it up."""
    from app import ai_filter
    from app.models import Keyword, NotionConfig
    from datetime import datetime as _dt, timezone as _tz

    aid = database.insert_article(
        url="https://example.com/n1",
        title="Weekly digest",  # no keyword in the subject
        source_name="newsletter",
        source_type="newsletter",
        country=None,
        published_at=_dt.now(_tz.utc),
        snippet="Berlin plant neues Förderprogramm für Elektrobusse.",
        language=None,
        outlook_entry_id="E1",
        outlook_store_id="S1",
    )
    assert aid is not None

    kw = Keyword(
        id=1, suchbegriff="Elektrobus", prioritaet="High",
        markt=[], sprache="DE", themenbereich=[],
    )
    cfg = NotionConfig(keywords=[kw], feeds=[], outlets=[], negative_keywords=[])
    tokens = ai_filter._high_priority_tokens(cfg)

    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (aid,)).fetchone()

    assert ai_filter._passes_pre_screen(row, tokens, config=cfg) is True


# ---------------------------------------------------------------------------
# v0.3.2 — /outlook/open/{id} route
# ---------------------------------------------------------------------------


def test_outlook_open_route_404_for_unknown_article():
    client = TestClient(app)
    r = client.post("/outlook/open/999999")
    assert r.status_code == 404


def test_outlook_open_route_rejects_non_newsletter():
    from datetime import datetime as _dt, timezone as _tz

    aid = database.insert_article(
        url="https://web.example.com/x", title="RSS article",
        source_name="rss", source_type="rss",
        country=None, published_at=_dt.now(_tz.utc), snippet=None,
    )
    client = TestClient(app)
    r = client.post(f"/outlook/open/{aid}")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "newsletter" in body["error"].lower()


def test_outlook_open_route_hint_when_no_entry_id():
    from datetime import datetime as _dt, timezone as _tz

    aid = database.insert_article(
        url="https://legacy.example.com/n", title="Legacy newsletter",
        source_name="mail", source_type="newsletter",
        country=None, published_at=_dt.now(_tz.utc), snippet=None,
        # No outlook_entry_id — simulates v0.3.0/v0.3.1 legacy row.
    )
    client = TestClient(app)
    r = client.post(f"/outlook/open/{aid}")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    # Hint should surface the subject so the user can find it manually.
    assert "Legacy newsletter" in body["error"]


def test_outlook_open_route_happy_path(monkeypatch):
    from datetime import datetime as _dt, timezone as _tz

    aid = database.insert_article(
        url="https://example.com/n2", title="Real newsletter",
        source_name="mail", source_type="newsletter",
        country=None, published_at=_dt.now(_tz.utc), snippet="body",
        outlook_entry_id="ENTRY-XYZ", outlook_store_id="STORE-XYZ",
    )
    captured = {}

    async def fake_open(*, entry_id, store_id=None):
        captured["entry_id"] = entry_id
        captured["store_id"] = store_id
        return {"ok": True}

    monkeypatch.setattr(outlook_poller, "open_item", fake_open)

    client = TestClient(app)
    r = client.post(f"/outlook/open/{aid}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured["entry_id"] == "ENTRY-XYZ"
    assert captured["store_id"] == "STORE-XYZ"


def test_open_item_empty_entry_id_returns_error():
    result = asyncio.run(outlook_poller.open_item(entry_id="", store_id=None))
    assert result["ok"] is False
    assert "EntryID" in result["error"]


def test_open_item_happy_path_with_mock_com(monkeypatch):
    """Mock the COM Dispatch so we can verify GetItemFromID + Display
    are called with the right args without touching real Outlook."""
    calls = {"get_item": None, "display": 0}

    class _FakeMail:
        def Display(self):
            calls["display"] += 1

    class _FakeNamespace2:
        def GetItemFromID(self, entry_id, store_id=None):
            calls["get_item"] = (entry_id, store_id)
            return _FakeMail()

    class _FakeApp:
        def GetNamespace(self, kind):
            assert kind == "MAPI"
            return _FakeNamespace2()

    class _FakeDispatchMod:
        @staticmethod
        def Dispatch(name):
            assert name == "Outlook.Application"
            return _FakeApp()

    monkeypatch.setitem(sys.modules, "pythoncom", _FakePythoncom())
    fake_win32com = types.SimpleNamespace(client=_FakeDispatchMod())
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", _FakeDispatchMod())

    result = asyncio.run(
        outlook_poller.open_item(entry_id="ENTRY-1", store_id="STORE-1")
    )
    assert result == {"ok": True}
    assert calls["get_item"] == ("ENTRY-1", "STORE-1")
    assert calls["display"] == 1
