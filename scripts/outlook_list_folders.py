"""One-off: enumerate every folder in the local Outlook desktop app so
you can copy the exact path into the wizard's Folder path field.

Usage:
    venv\\Scripts\\python.exe scripts\\outlook_list_folders.py

Prerequisites: Outlook is installed, logged in, and running (or COM
will start it silently). No IMAP password / OAuth needed — this uses
the same COM API the poller uses.

Example output:

    Store: jordan.kern@flixbus.com
      Posteingang
      Posteingang/BT Verkehr
      Posteingang/BT Verkehr/Newsletters
      Gesendete Elemente
      Entwürfe
      ...

Copy whichever line matches your target folder (without the "Store:"
prefix) into the wizard's Folder path field. If Outlook has multiple
stores, note the store name too.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _walk(folder, prefix: str, out: list) -> None:
    """Depth-first walk. Emits ``prefix + folder.Name`` for the current
    folder, then recurses into its ``Folders`` collection."""
    name = str(getattr(folder, "Name", "") or "")
    path = f"{prefix}/{name}" if prefix else name
    out.append(path)
    try:
        for sub in folder.Folders:
            _walk(sub, path, out)
    except Exception:
        pass  # some pseudo-folders can't be iterated — silently skip


def main() -> int:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        print(
            "pywin32 is not available in this environment.\n"
            "This script only works on Windows with pywin32 installed."
        )
        return 1

    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
        except Exception as exc:
            print(f"Cannot open Outlook via COM: {exc}")
            print("Is the Outlook desktop app installed and running?")
            return 2

        # Iterate every store (top-level Namespace.Folders entry).
        try:
            stores = list(namespace.Folders)
        except Exception as exc:
            print(f"Cannot enumerate stores: {exc}")
            return 3

        print()
        print("=" * 60)
        print("Folders in your local Outlook (copy one into the wizard):")
        print("=" * 60)
        for store in stores:
            store_name = str(getattr(store, "Name", "") or "?")
            print()
            print(f"Store: {store_name}")
            paths: list = []
            try:
                for top in store.Folders:
                    _walk(top, "", paths)
            except Exception as exc:
                print(f"  (could not enumerate: {exc})")
                continue
            for p in paths:
                print(f"  {p}")

        print()
        print("Fill the exact path (without the 'Store:' prefix) into")
        print("'Folder path' in the wizard. If you have more than one store,")
        print("also fill 'Store / mailbox' with the store name.")
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
