"""Launch approved profiles and track observed processes without blocking the interface."""

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

import psutil

from .discovery import checked_path, path_key

log = logging.getLogger(__name__)


class Playtime:
    def __init__(self, db):
        self.db = db
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.children = {}

    def start(self):
        self.thread = threading.Thread(target=self._work, name="aether-playtime", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(5)

    def _work(self):
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("Unable to update play sessions")
            self.stop_event.wait(2)

    def launch(self, profile_id):
        with self.lock:
            rows = self.db.rows(
                """SELECT p.*,i.game,i.path,i.status,f.enabled,f.status AS source_status
                FROM profiles p JOIN installations i ON i.id=p.installation JOIN folders f ON f.id=i.folder WHERE p.id=?""",
                (profile_id,),
            )
            if not rows:
                raise ValueError("Approve a launch profile first.")
            p = rows[0]
            if not p["enabled"] or p["source_status"] == "Offline" or p["status"] in {"Missing", "Ignored"}:
                raise ValueError("This game source is unavailable.")
            root = Path(p["path"])
            exe = checked_path(root, p["executable"])
            cwd = checked_path(root, p["cwd"], directory=True)
            self.poll()
            if self.db.rows(
                "SELECT 1 FROM sessions WHERE installation=? AND end IS NULL", (p["installation"],)
            ):
                raise ValueError("This installation is already running.")
            child = subprocess.Popen([str(exe), *json.loads(p["arguments"])], cwd=cwd, shell=False)
            self.children[p["installation"]] = child
            now = time.time()
            try:
                born = psutil.Process(child.pid).create_time()
            except psutil.Error:
                born = now
            self._begin(p["installation"], child.pid, born, now)

    def _begin(self, iid, pid, born, start):
        with self.db.connection(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions(installation,start,seen,pid,born) VALUES(?,?,?,?,?)",
                (iid, start, start, pid, born),
            )

    def poll(self):
        with self.lock:
            live = {s["installation"]: s for s in self.db.rows("SELECT * FROM sessions WHERE end IS NULL")}
            profiles = self.db.rows("""SELECT p.installation,p.executable,i.path FROM profiles p
                JOIN installations i ON i.id=p.installation JOIN folders f ON f.id=i.folder
                WHERE f.enabled AND i.status NOT IN ('Ignored','Missing') AND f.status!='Offline' ORDER BY p.id""")
            if not live and not profiles:
                return
            approved = {path_key(Path(p["path"]) / p["executable"]): p["installation"] for p in profiles}
            roots = {p["installation"]: Path(p["path"]) for p in profiles}
            active = {}
            now = time.time()
            external = self.db.setting("external", "on") == "on"
            # Extend a tracked launch to a child game process in the same installation (common with launchers).
            for iid, entry in live.items():
                try:
                    process = psutil.Process(entry["pid"])
                    if (
                        abs(process.create_time() - entry["born"]) > 0.05
                        or process.status() == psutil.STATUS_ZOMBIE
                    ):
                        continue
                    active[iid] = (process.pid, entry["born"])
                    root = roots.get(iid)
                    if root:
                        for child in process.children(recursive=True):
                            try:
                                child_path = Path(child.exe())
                                if child_path.is_relative_to(root) and path_key(child_path) in approved:
                                    active[iid] = (child.pid, child.create_time())
                                    break
                            except psutil.Error:
                                continue
                except psutil.Error:
                    continue
            if external or live:
                for process in psutil.process_iter(["exe", "create_time", "status"], ad_value=None):
                    try:
                        info = process.info
                        exe = info["exe"]
                        if not exe or not info["create_time"] or info["status"] == psutil.STATUS_ZOMBIE:
                            continue
                        iid = approved.get(path_key(Path(exe)))
                        if iid and (external or iid in live):
                            active.setdefault(iid, (process.pid, info["create_time"]))
                    except (psutil.Error, OSError):
                        continue
            for iid, (pid, born) in active.items():
                if iid not in live:
                    self._begin(iid, pid, born, now)
                else:
                    with self.db.connection(True, notify=False) as db:
                        db.execute(
                            "UPDATE sessions SET seen=?,pid=?,born=? WHERE id=?",
                            (now, pid, born, live[iid]["id"]),
                        )
            for iid in live.keys() - active.keys():
                entry = live[iid]
                # A crash or shutdown must never turn a stale session into days of invented playtime.
                end = now if now - entry["seen"] < 8 else entry["seen"]
                with self.db.connection(True) as db:
                    db.execute("UPDATE sessions SET end=?,seen=? WHERE id=?", (end, end, entry["id"]))
            for iid, child in list(self.children.items()):
                if child.poll() is not None:
                    del self.children[iid]
