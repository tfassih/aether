import os
import shutil
import time
from pathlib import Path

import pytest

from aether.database import Database
from aether.discovery import checked_path
from aether.monitor import Monitor, scan
from aether.playtime import Playtime


def put(path, data=b"game"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def source(tmp_path):
    lib = Database(tmp_path / "Data")
    root = tmp_path / "Offline games"
    root.mkdir()
    lib.add_folder(root)
    return lib, root, lib.folders()[0]


def until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := predicate():
            return value
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for observed state")


def test_discovery_ranks_without_approving_and_is_idempotent(source):
    db, root, f = source
    exe = put(root / "Moonrise" / "Moonrise.exe")
    put(exe.parent / "bin" / "Launcher.exe")
    put(exe.parent / "unins000.exe")
    put(exe.parent / "_CommonRedist" / "vcredist_x86.exe")
    put(root / "Tools" / "setup.exe")
    for _ in range(2):
        scan(db, f)
    (game,) = db.library()
    (inst,) = db.installations(game["id"])
    assert inst["candidates"] == ["Moonrise.exe", "bin/Launcher.exe"]
    assert db.profiles(inst["id"]) == []
    assert game["installed"] and db.folders()[0]["status"] == "Online"
    assert Database(db.home).game(game["id"])["title"] == "Moonrise"
    with pytest.raises(ValueError, match="Approve"):
        Playtime(db).launch(999)


def test_move_missing_offline_restore_and_edits(source):
    db, root, folder = source
    exe = put(root / "Orbit" / "Orbit.exe")
    scan(db, folder)
    (g,) = db.library()
    (i,) = db.installations(g["id"])
    db.approve(i["id"], "Main", "Orbit.exe", ["--full screen"])
    db.edit_game(g["id"], {"title": "My Orbit", "notes": "A note"}, ["RPG"], ["Backlog"])
    db.favorite(g["id"])
    db.add_art(g["id"], "cover", "mine.webp")
    exe.parent.rename(root / "Orbit Remastered")
    scan(db, folder)
    assert db.installations(g["id"])[0]["id"] == i["id"]
    assert db.profiles(i["id"])[0]["arguments"] == ["--full screen"]
    root.rename(root.with_name("Detached"))
    scan(db, folder)
    assert db.folders()[0]["status"] == "Offline"
    assert not db.game(g["id"])["installed"]
    root.with_name("Detached").rename(root)
    scan(db, folder)
    assert db.game(g["id"])["installed"]
    shutil.rmtree(root / "Orbit Remastered")
    scan(db, folder)
    game = db.game(g["id"])
    assert (game["title"], game["notes"], game["favorite"], game["collections"], game["cover"]) == (
        "My Orbit",
        "A note",
        1,
        ["Backlog"],
        "mine.webp",
    )
    assert db.installations(g["id"])[0]["status"] == "Missing"


def test_source_removal_exclusions_depth_and_readd(source):
    db, root, f = source
    put(root / "A" / "a.exe")
    put(root / "B" / "bin" / "b.exe")
    scan(db, f)
    db.configure_folder(f["id"], True, 0, ["A"])
    scan(db, db.folders()[0])
    assert not any(g["installed"] for g in db.library())
    with pytest.raises(ValueError):
        db.add_folder(root / "A")
    db.remove_folder(f["id"])
    assert not db.folders() and len(db.library()) == 2
    assert db.add_folder(root) == f["id"]
    db.configure_folder(f["id"], True, 6, [])
    scan(db, db.folders()[0])
    assert len(db.library()) == 2 and all(g["installed"] for g in db.library())


def test_single_install_manual_profile_and_path_validation(tmp_path):
    db = Database(tmp_path / "Data")
    exe = put(tmp_path / "My Game" / "bin" / "Game.exe")
    db.add_folder(exe.parent.parent, "single")
    scan(db, db.folders()[0])
    (g,) = db.library()
    (i,) = db.installations(g["id"])
    pid = db.approve(i["id"], "DX12", "bin/Game.exe", ["--renderer=dx12", "a b"], "bin")
    assert db.profiles(i["id"])[0]["id"] == pid
    for path in ("../outside.exe", str(tmp_path / "outside.exe"), "bin"):
        with pytest.raises((ValueError, OSError)):
            checked_path(Path(i["path"]), path)
    exe.unlink()
    with pytest.raises((ValueError, OSError)):
        Playtime(db).launch(pid)
    scan(db, db.folders()[0])
    assert not db.game(g["id"])["installed"]
    db.revoke(pid)
    assert not db.profiles(i["id"])


def test_real_watcher_create_rename_delete(source):
    db, root, _ = source
    monitor = Monitor(db)
    monitor.start()
    try:
        until(lambda: db.folders()[0]["status"] == "Online")
        exe = put(root / "Delta" / "Delta.exe")
        until(lambda: len(db.library()) == 1)
        gid = db.library()[0]["id"]
        exe.parent.rename(root / "Renamed")
        until(lambda: db.installations(gid)[0]["path"] == str(root / "Renamed"))
        (root / "Renamed" / "Delta.exe").unlink()
        until(lambda: db.installations(gid)[0]["status"] == "No executable")
    finally:
        monitor.stop()


def test_real_launch_and_external_session(source):
    db, root, f = source
    exe = root / "Process fixture" / "Probe.exe"
    exe.parent.mkdir()
    if os.name == "nt":
        shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "ping.exe", exe)
        args = ["-n", "3", "127.0.0.1"]
    else:
        shutil.copy2(shutil.which("sleep"), exe)
        args = ["2"]
    scan(db, f)
    game = db.library()[0]
    iid = db.installations(game["id"])[0]["id"]
    profile = db.approve(iid, "Test", "Probe.exe", args)
    tracker = Playtime(db)
    tracker.launch(profile)
    with pytest.raises(ValueError, match="already running"):
        tracker.launch(profile)

    def finished():
        tracker.poll()
        return db.rows("SELECT * FROM sessions WHERE end IS NOT NULL")

    until(finished)
    assert db.game(game["id"])["seconds"] >= 1
    import subprocess

    with subprocess.Popen([str(exe), *args]) as outside:
        until(lambda: (tracker.poll(), db.game(game["id"])["playing"])[1])
        outside.wait(timeout=10)
    tracker.poll()
    assert len(db.rows("SELECT id FROM sessions WHERE end IS NOT NULL")) == 2


def test_stale_sessions_end_at_last_observation(source):
    db, root, f = source
    put(root / "Nova" / "Nova.exe")
    scan(db, f)
    iid = db.installations(db.library()[0]["id"])[0]["id"]
    with db.connection(True) as conn:
        conn.execute(
            "INSERT INTO sessions(installation,start,seen,pid,born) VALUES(?,?,?,?,?)",
            (iid, 10.0, 15.0, 9999999, 10.0),
        )
    Playtime(db).poll()
    assert db.library()[0]["seconds"] == 5
    assert db.rows("SELECT end FROM sessions")[0]["end"] == 15


def test_partial_scan_and_stale_observation(source, monkeypatch):
    from aether import monitor

    db, root, f = source
    put(root / "Game" / "Game.exe")
    scan(db, f)

    def deny(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(monitor, "observe", deny)
    scan(db, f)
    assert db.folders()[0]["status"] == "Partial"
    assert db.library()[0]["installed"]
    db.configure_folder(f["id"], False, 3, [])
    scan(db, f)
    assert db.folders()[0]["status"] == "Paused"


def test_restarted_game_does_not_bridge_stale_playtime(source, monkeypatch):
    from types import SimpleNamespace

    import psutil

    db, root, f = source
    exe = put(root / "Nova" / "Nova.exe")
    scan(db, f)
    iid = db.installations(db.library()[0]["id"])[0]["id"]
    db.approve(iid, "Main", "Nova.exe", [])
    with db.connection(True) as conn:
        conn.execute(
            "INSERT INTO sessions(installation,start,seen,pid,born) VALUES(?,?,?,?,?)",
            (iid, 10.0, 15.0, 9999999, 10.0),
        )
    process = SimpleNamespace(
        pid=98765, info={"exe": str(exe), "create_time": time.time() - 1, "status": "running"}
    )
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **kw: [process])
    Playtime(db).poll()
    rows = db.rows("SELECT start,end FROM sessions ORDER BY id")
    assert rows[0] == {"start": 10.0, "end": 15.0}
    assert len(rows) == 2 and rows[1]["end"] is None
    assert db.library()[0]["seconds"] == 5


def test_source_disconnect_during_scan_retains_installation(source, monkeypatch):
    from aether import monitor

    db, root, f = source
    put(root / "Nova" / "Nova.exe")
    scan(db, f)

    def unplug(*args):
        root.rename(root.with_name("Disconnected"))
        raise FileNotFoundError("source disconnected")

    monkeypatch.setattr(monitor, "observe", unplug)
    scan(db, f)
    game = db.library()[0]
    assert db.folders()[0]["status"] == "Offline" and not game["installed"]
    assert db.installations(game["id"])[0]["status"] == "Installed"
