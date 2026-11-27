"""Per-user SQLite catalog. User edits and filesystem observations have separate owners."""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .discovery import Observation, checked_path, linked, path_key

SCHEMA = """
CREATE TABLE folders(id INTEGER PRIMARY KEY, path TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL, depth INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'Pending', scanned REAL, removed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE ignored_paths(folder INTEGER REFERENCES folders, path TEXT NOT NULL, PRIMARY KEY(folder,path));
CREATE TABLE games(id INTEGER PRIMARY KEY, detected TEXT NOT NULL, rawg_id INTEGER UNIQUE, rawg TEXT NOT NULL DEFAULT '{}',
    edits TEXT NOT NULL DEFAULT '{}', favorite INTEGER NOT NULL DEFAULT 0, match_state TEXT NOT NULL DEFAULT 'New',
    attempted REAL DEFAULT 0, updated REAL);
CREATE TABLE installations(id INTEGER PRIMARY KEY, game INTEGER NOT NULL REFERENCES games,
    folder INTEGER NOT NULL REFERENCES folders, path TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
    identity TEXT NOT NULL, candidates TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL, verified REAL);
CREATE INDEX install_identity ON installations(identity);
CREATE TABLE profiles(id INTEGER PRIMARY KEY, installation INTEGER NOT NULL REFERENCES installations,
    name TEXT NOT NULL, executable TEXT NOT NULL, arguments TEXT NOT NULL, cwd TEXT NOT NULL,
    UNIQUE(installation,name));
CREATE TABLE sessions(id INTEGER PRIMARY KEY, installation INTEGER NOT NULL REFERENCES installations,
    start REAL NOT NULL, seen REAL NOT NULL, end REAL, pid INTEGER NOT NULL, born REAL NOT NULL);
CREATE UNIQUE INDEX live_session ON sessions(installation) WHERE end IS NULL;
CREATE TABLE groups(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE, UNIQUE(kind,name));
CREATE TABLE memberships(game INTEGER REFERENCES games ON DELETE CASCADE, group_id INTEGER REFERENCES groups ON DELETE CASCADE,
    PRIMARY KEY(game,group_id));
CREATE TABLE artwork(id INTEGER PRIMARY KEY, game INTEGER REFERENCES games ON DELETE CASCADE, kind TEXT NOT NULL,
    file TEXT NOT NULL, source TEXT NOT NULL, UNIQUE(game,kind,file,source));
CREATE TABLE cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL, saved REAL NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE scans(id INTEGER PRIMARY KEY, folder INTEGER NOT NULL REFERENCES folders, time REAL NOT NULL,
    status TEXT NOT NULL, message TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, home: Path):
        self.home = home
        self.art = home / "artwork"
        self.art.mkdir(parents=True, exist_ok=True)
        self.file = home / "library.sqlite3"
        self.revision = 0
        self._lock = threading.Lock()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            elif version != 1:
                raise ValueError("This library was created by a newer version of Aether.")

    @contextmanager
    def connection(self, write=False, *, notify=True):
        db = sqlite3.connect(self.file, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            changed = db.total_changes
            db.commit()
            if write and notify and changed:
                with self._lock:
                    self.revision += 1
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connection() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def setting(self, key, default=""):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key, value):
        with self.connection(True, notify=False) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))

    def folders(self):
        return self.rows("SELECT * FROM folders WHERE removed=0 ORDER BY path COLLATE NOCASE")

    def add_folder(self, path, mode="library", depth=6):
        if not str(path).strip():
            raise ValueError("Enter a folder path.")
        p = Path(os.path.expandvars(str(path).strip().strip('"'))).expanduser().absolute()
        if not p.is_dir() or any(linked(d) for d in (*p.parents, p)):
            raise ValueError("Choose an accessible folder outside links and junctions.")
        if mode not in {"library", "single"} or not 0 <= int(depth) <= 16:
            raise ValueError("Choose a library or game folder and a depth between 0 and 16.")
        key = path_key(p)
        with self.connection(True) as db:
            for row in db.execute("SELECT key FROM folders WHERE removed=0"):
                other = Path(row["key"])
                if Path(key).is_relative_to(other) or other.is_relative_to(Path(key)):
                    raise ValueError(
                        "This folder overlaps a folder already in the library. Adjust the existing source."
                    )
            return db.execute(
                """INSERT INTO folders(path,key,mode,depth) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                mode=excluded.mode,depth=excluded.depth,enabled=1,removed=0,generation=generation+1,status='Pending' RETURNING id""",
                (str(p), key, mode, int(depth)),
            ).fetchone()[0]

    def remove_folder(self, fid):
        with self.connection(True) as db:
            db.execute(
                "UPDATE folders SET removed=1,enabled=0,status='Removed',generation=generation+1 WHERE id=?",
                (fid,),
            )

    def configure_folder(self, fid, enabled, depth, exclusions):
        if not 0 <= int(depth) <= 16:
            raise ValueError("Scan depth must be between 0 and 16.")
        paths = set()
        for value in exclusions:
            rel = Path(value.strip())
            if not rel.parts:
                continue
            if rel.is_absolute() or rel.drive or ".." in rel.parts:
                raise ValueError("Exclusions must be relative to this source.")
            paths.add(rel.as_posix())
        with self.connection(True) as db:
            db.execute(
                "UPDATE folders SET enabled=?, depth=?, status=?,generation=generation+1 WHERE id=?",
                (int(enabled), int(depth), "Pending" if enabled else "Paused", fid),
            )
            db.execute("DELETE FROM ignored_paths WHERE folder=?", (fid,))
            db.executemany("INSERT INTO ignored_paths VALUES(?,?)", [(fid, p) for p in paths])

    def exclusions(self, fid):
        return [
            r["path"]
            for r in self.rows("SELECT path FROM ignored_paths WHERE folder=? ORDER BY path", (fid,))
        ]

    def scan_result(self, folder, observations, absent, ignored, status, message, *, full):
        now = time.time()
        with self.connection(True) as db:
            current = db.execute("SELECT * FROM folders WHERE id=?", (folder["id"],)).fetchone()
            if not current or not current["enabled"] or current["generation"] != folder["generation"]:
                return
            for key in absent:
                db.execute(
                    "UPDATE installations SET status='Missing' WHERE folder=? AND key=?", (folder["id"], key)
                )
            for key in ignored:
                db.execute(
                    "UPDATE installations SET status='Ignored' WHERE folder=? AND key=?", (folder["id"], key)
                )
            for obs in observations:
                self._reconcile(db, folder["id"], obs, now)
            if full or status != "Online" or current["status"] != "Partial":
                db.execute("UPDATE folders SET status=?,scanned=? WHERE id=?", (status, now, folder["id"]))
            db.execute(
                "INSERT INTO scans(folder,time,status,message) VALUES(?,?,?,?)",
                (folder["id"], now, status, message),
            )
            db.execute("DELETE FROM scans WHERE id <= (SELECT MAX(id)-300 FROM scans)")

    @staticmethod
    def _reconcile(db, fid, obs: Observation, now):
        key = path_key(obs.path)
        existing = db.execute("SELECT * FROM installations WHERE key=?", (key,)).fetchone()
        if not existing and obs.identity:
            previous = db.execute(
                "SELECT * FROM installations WHERE identity=? AND key!=?", (obs.identity, key)
            ).fetchall()
            # A move is conclusive only if the old parent is accessible and the old path is gone.
            previous = [
                p
                for p in previous
                if Path(p["path"]).parent.is_dir()
                and not Path(p["path"]).exists()
                and set(json.loads(p["candidates"])).intersection(obs.candidates)
            ]
            if len(previous) == 1:
                existing = previous[0]
        if not obs.candidates and not existing:
            return
        state = "Installed" if obs.candidates else "No executable"
        fields = (fid, str(obs.path), key, obs.identity, json.dumps(obs.candidates), obs.version, state, now)
        if existing:
            db.execute(
                "UPDATE installations SET folder=?,path=?,key=?,identity=?,candidates=?,version=?,status=?,verified=? WHERE id=?",
                (*fields, existing["id"]),
            )
        else:
            gid = db.execute("INSERT INTO games(detected) VALUES(?) RETURNING id", (obs.title,)).fetchone()[0]
            db.execute(
                "INSERT INTO installations(folder,path,key,identity,candidates,version,status,verified,game) VALUES(?,?,?,?,?,?,?,?,?)",
                (*fields, gid),
            )

    def installations(self, gid):
        rows = self.rows(
            """SELECT i.*, f.status AS source_status, f.enabled FROM installations i
            JOIN folders f ON f.id=i.folder WHERE i.game=? ORDER BY i.path""",
            (gid,),
        )
        for row in rows:
            row["candidates"] = json.loads(row["candidates"])
        return rows

    def profiles(self, iid):
        rows = self.rows("SELECT * FROM profiles WHERE installation=? ORDER BY name", (iid,))
        for row in rows:
            row["arguments"] = json.loads(row["arguments"])
        return rows

    def approve(self, iid, name, executable, arguments, cwd="."):
        if not name.strip():
            raise ValueError("Name this launch profile.")
        inst = self.rows("SELECT path FROM installations WHERE id=?", (iid,))[0]
        root = Path(inst["path"])
        checked_path(root, executable)
        checked_path(root, cwd or ".", directory=True)
        if not all(isinstance(arg, str) and "\x00" not in arg for arg in arguments):
            raise ValueError("Each launch argument must be a line of plain text.")
        with self.connection(True) as db:
            return db.execute(
                """INSERT INTO profiles(installation,name,executable,arguments,cwd) VALUES(?,?,?,?,?)
                ON CONFLICT(installation,name) DO UPDATE SET executable=excluded.executable,arguments=excluded.arguments,cwd=excluded.cwd
                RETURNING id""",
                (iid, name.strip(), Path(executable).as_posix(), json.dumps(arguments), cwd or "."),
            ).fetchone()[0]

    def revoke(self, profile):
        with self.connection(True) as db:
            db.execute("DELETE FROM profiles WHERE id=?", (profile,))

    def library(self):
        rows = self.rows("""SELECT g.*,
            (SELECT COUNT(*) FROM installations i WHERE i.game=g.id) AS copies,
            EXISTS(SELECT 1 FROM installations i JOIN folders f ON f.id=i.folder WHERE i.game=g.id
                AND f.enabled AND f.status!='Offline' AND i.status='Installed') AS installed,
            COALESCE((SELECT SUM(MAX(0,s.seen-s.start)) FROM sessions s JOIN installations i ON s.installation=i.id WHERE i.game=g.id),0) AS seconds,
            (SELECT MAX(s.start) FROM sessions s JOIN installations i ON s.installation=i.id WHERE i.game=g.id) AS last_played,
            EXISTS(SELECT 1 FROM sessions s JOIN installations i ON s.installation=i.id WHERE i.game=g.id AND s.end IS NULL) AS playing,
            (SELECT file FROM artwork a WHERE a.game=g.id AND kind IN ('cover','background')
                ORDER BY (kind='cover') DESC,(source='User') DESC,a.id DESC LIMIT 1) AS cover
            FROM games g""")
        memberships = {}
        for item in self.rows(
            "SELECT m.game,g.kind,g.name FROM memberships m JOIN groups g ON g.id=m.group_id ORDER BY g.name COLLATE NOCASE"
        ):
            memberships.setdefault((item["game"], item["kind"]), []).append(item["name"])
        for r in rows:
            raw, edits = json.loads(r["rawg"]), json.loads(r["edits"])
            r.update(
                {
                    "title": r["detected"],
                    "description": "",
                    "genres": [],
                    "platforms": [],
                    "released": "",
                    "rating": None,
                    "notes": "",
                    "developer": "",
                    "publisher": "",
                }
            )
            r.update(raw)
            r.update(edits)
            r["tags"] = memberships.get((r["id"], "tag"), [])
            r["collections"] = memberships.get((r["id"], "collection"), [])
        return rows

    def game(self, gid):
        return next((r for r in self.library() if r["id"] == gid), None)

    def groups(self, kind="collection"):
        return self.rows(
            """SELECT g.id,g.name,COUNT(m.game) AS amount FROM groups g LEFT JOIN memberships m ON m.group_id=g.id
            WHERE g.kind=? GROUP BY g.id ORDER BY g.name COLLATE NOCASE""",
            (kind,),
        )

    def create_group(self, name, kind="collection"):
        name = name.strip()
        if not name:
            raise ValueError("Enter a name.")
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO groups(kind,name) VALUES(?,?)", (kind, name))

    def delete_group(self, group_id):
        with self.connection(True) as db:
            db.execute("DELETE FROM groups WHERE id=?", (group_id,))

    def edit_game(self, gid, edits, tags, collections):
        with self.connection(True) as db:
            db.execute("UPDATE games SET edits=? WHERE id=?", (json.dumps(edits), gid))
            db.execute("DELETE FROM memberships WHERE game=?", (gid,))
            for kind, values in [("tag", tags), ("collection", collections)]:
                for val in {str(v).strip() for v in values if str(v).strip()}:
                    db.execute("INSERT OR IGNORE INTO groups(kind,name) VALUES(?,?)", (kind, val))
                    db.execute(
                        "INSERT OR IGNORE INTO memberships SELECT ?,id FROM groups WHERE kind=? AND name=?",
                        (gid, kind, val),
                    )

    def favorite(self, gid):
        with self.connection(True) as db:
            db.execute("UPDATE games SET favorite=1-favorite WHERE id=?", (gid,))

    def gallery(self, gid):
        return self.rows("SELECT * FROM artwork WHERE game=? ORDER BY (source='User') DESC,id DESC", (gid,))

    def add_art(self, gid, kind, file, source="User"):
        with self.connection(True) as db:
            if kind != "screenshot":
                db.execute("DELETE FROM artwork WHERE game=? AND kind=? AND source=?", (gid, kind, source))
            db.execute(
                "INSERT OR IGNORE INTO artwork(game,kind,file,source) VALUES(?,?,?,?)",
                (gid, kind, file, source),
            )

    def apply_match(self, gid, rawg_id, data):
        with self.connection(True) as db:
            old = db.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
            if old is None:
                raise ValueError("The library changed. Open the game again.")
            target = db.execute("SELECT * FROM games WHERE rawg_id=? AND id!=?", (rawg_id, gid)).fetchone()
            if target:
                # An explicit common RAWG identity unifies copies and keeps their profiles and sessions.
                target_id = target["id"]
                db.execute("UPDATE installations SET game=? WHERE game=?", (target_id, gid))
                db.execute(
                    "INSERT OR IGNORE INTO memberships SELECT ?,group_id FROM memberships WHERE game=?",
                    (target_id, gid),
                )
                db.execute(
                    "INSERT OR IGNORE INTO artwork(game,kind,file,source) SELECT ?,kind,file,source FROM artwork WHERE game=? AND source='User'",
                    (target_id, gid),
                )
                edits = json.loads(old["edits"]) | json.loads(target["edits"])
                db.execute(
                    "UPDATE games SET favorite=MAX(favorite,?),edits=? WHERE id=?",
                    (old["favorite"], json.dumps(edits), target_id),
                )
                db.execute("DELETE FROM games WHERE id=?", (gid,))
                gid = target_id
            if old["rawg_id"] != rawg_id and not target:
                db.execute("DELETE FROM artwork WHERE game=? AND source='RAWG'", (gid,))
            db.execute(
                "UPDATE games SET rawg_id=?,rawg=?,match_state='Matched',updated=? WHERE id=?",
                (rawg_id, json.dumps(data), time.time(), gid),
            )
        return gid

    def cached(self, key):
        rows = self.rows("SELECT payload,saved FROM cache WHERE key=?", (key,))
        return (json.loads(rows[0]["payload"]), rows[0]["saved"]) if rows else (None, 0)

    def cache(self, key, payload):
        with self.connection(True, notify=False) as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, json.dumps(payload), time.time()))
