"""Coalesce filesystem events, then reconcile one installation or one source at a time."""

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .discovery import SKIP_DIRS, linked, observe, path_key

log = logging.getLogger(__name__)


def scan(db, folder, parts=None):
    root = Path(folder["path"])
    fid = folder["id"]
    excluded = {path_key(root / p) for p in db.exclusions(fid)}
    existing = {row["key"] for row in db.rows("SELECT key FROM installations WHERE folder=?", (fid,))}
    try:
        if linked(root):
            raise OSError("Source is a link or junction")
        if folder["mode"] == "single":
            targets = [root]
        elif parts is None:
            targets = list(root.iterdir())
        else:
            targets = [root / name for name in parts]
        if not root.is_dir():
            raise FileNotFoundError(root)
    except OSError as exc:
        db.scan_result(folder, [], [], [], "Offline", str(exc), full=True)
        return
    seen, ignored, observations, failures = set(), set(), [], []
    for p in targets:
        key = path_key(p)
        try:
            if key in excluded or p.name.casefold() in SKIP_DIRS or linked(p):
                ignored.add(key)
                continue
            if not p.is_dir():
                continue
            seen.add(key)
            observations.append(observe(p, folder["depth"], excluded))
        except FileNotFoundError:
            # A subtree can change while walking. Preserve its previous state until the next observation.
            if p.exists():
                seen.add(key)
                failures.append(f"{p.name} changed during scanning")
        except OSError as exc:
            seen.add(key)
            failures.append(f"{p.name}: {exc}")
    if not root.is_dir():
        db.scan_result(folder, [], [], [], "Offline", "Source disconnected during the scan", full=True)
        return
    scope = (
        existing if parts is None or folder["mode"] == "single" else existing & {path_key(p) for p in targets}
    )
    missing = scope - seen - ignored
    count = sum(bool(o.candidates) for o in observations)
    message = f"{count} game installation{'s' if count != 1 else ''} found in scan scope"
    if missing:
        message += f", {len(missing)} missing"
    if failures:
        message += ". " + " · ".join(failures[:4])
    db.scan_result(
        folder,
        observations,
        missing,
        ignored,
        "Partial" if failures else "Online",
        message,
        full=parts is None,
    )


class SourceEvents(FileSystemEventHandler):
    def __init__(self, folder, queue):
        self.folder = folder
        self.root = Path(folder["path"])
        self.queue = queue

    def on_any_event(self, event):
        if event.event_type not in {"created", "deleted", "modified", "moved"}:
            return
        if not event.is_directory and not any(
            str(p).lower().endswith(".exe") for p in (event.src_path, event.dest_path)
        ):
            return
        # Directory mtime changes accompany writes to ordinary files. Real directory create/move/delete
        # and .exe changes already carry the child we need.
        if event.is_directory and event.event_type == "modified":
            return
        for value in (event.src_path, event.dest_path):
            if not value:
                continue
            try:
                rel = Path(value).relative_to(self.root)
            except ValueError:
                continue
            child = rel.parts[0] if rel.parts and self.folder["mode"] == "library" else None
            self.queue(self.folder["id"], child)


class Monitor:
    def __init__(self, db):
        self.db = db
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        self.observers = {}
        self.known = {}
        self.activity = ""
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._work, name="aether-discovery", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(8)

    def queue(self, fid, child=None, *, immediate=False):
        now = time.monotonic()
        with self.lock:
            first, _, names = self.pending.get(fid, (now, now, set()))
            if child is None or names is None or len(names) > 200:
                names = None
            else:
                names.add(child)
            self.pending[fid] = (first, now if immediate else min(first + 4, now + 0.7), names)
        self.wake.set()

    def rescan(self):
        for f in self.db.folders():
            if f["enabled"]:
                self.queue(f["id"], immediate=True)

    def _sync(self, folders, full):
        folders = list(folders)
        removed = self.known.keys() - {f["id"] for f in folders}
        for fid in removed:
            self.known.pop(fid, None)
            observer = self.observers.pop(fid, None)
            if observer:
                observer.stop()
                observer.join(2)
        for f in folders:
            fid, root = f["id"], Path(f["path"])
            online = f["enabled"] and root.is_dir()
            if fid in self.observers and not online:
                obs = self.observers.pop(fid)
                obs.stop()
                obs.join(2)
            if online and fid not in self.observers:
                observer = Observer()
                try:
                    observer.schedule(SourceEvents(f, self.queue), str(root), recursive=True)
                    observer.start()
                    self.observers[fid] = observer
                except OSError:
                    log.info("Native change notifications unavailable for %s; using reconciliation", root)
            state = (bool(online), bool(f["enabled"]), f["depth"])
            if f["enabled"] and (full or self.known.get(fid) != state):
                self.queue(fid, immediate=True)
            self.known[fid] = state

    def _work(self):
        next_check = next_full = 0
        try:
            while not self.stop_event.is_set():
                try:
                    now = time.monotonic()
                    folders = {f["id"]: f for f in self.db.folders()}
                    if now >= next_check:
                        self._sync(folders.values(), now >= next_full)
                        next_check = now + 4
                        if now >= next_full:
                            next_full = now + 180
                    with self.lock:
                        ready = {fid: item[2] for fid, item in self.pending.items() if item[1] <= now}
                        for fid in ready:
                            del self.pending[fid]
                    for fid, parts in ready.items():
                        if self.stop_event.is_set():
                            break
                        f = folders.get(fid)
                        if f and f["enabled"]:
                            self.activity = str(f["path"])
                            scan(self.db, f, parts)
                except Exception:
                    log.exception("Source reconciliation failed")
                finally:
                    self.activity = ""
                self.wake.wait(0.5)
                self.wake.clear()
        finally:
            for observer in self.observers.values():
                observer.stop()
            for observer in self.observers.values():
                observer.join(2)
