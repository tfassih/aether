import argparse
import asyncio
import ctypes
import json
import logging
import os
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from nicegui import app, native, ui
from platformdirs import user_data_path
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .database import Database
from .interface import Shell
from .metadata import Metadata
from .monitor import Monitor
from .playtime import Playtime


class LocalOrigin:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode("latin1")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    trusted = parsed.scheme == "http" and parsed.netloc == headers.get(b"host", b"").decode(
                        "latin1"
                    )
                except ValueError:
                    trusted = False
                if not trusted:
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        await send({"type": "http.response.start", "status": 403, "headers": []})
                        await send({"type": "http.response.body", "body": b"Local desktop requests only"})
                    return
        await self.app(scope, receive, send)


@contextmanager
def single_instance(home):
    home.mkdir(parents=True, exist_ok=True)
    with (home / "instance.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


async def smoke(result, native_window):
    """Exercise the visible packaged UI over its actual NiceGUI socket and native web engine."""
    outcome = {}
    try:
        client = ui.context.client
        await client.connected(timeout=30)

        async def wait_js(expression, timeout=30):
            for _ in range(timeout * 4):
                value = await client.run_javascript(expression, timeout=8)
                if value:
                    return value
                await asyncio.sleep(0.25)
            raise RuntimeError("UI did not reach expected state: " + expression[:120])

        def click(text):
            label = json.dumps(text)
            return f"(() => {{const b=[...document.querySelectorAll('button')].reverse().find(e=>(e.querySelector('.block')?.textContent || e.innerText.trim().split('\\n').pop()).toLowerCase()==={label}.toLowerCase() && e.offsetWidth); if(b){{b.click();return true}}return false}})()"

        await wait_js("document.querySelector('.page-title')?.textContent === 'Game library'")
        outcome["engine"] = await wait_js("navigator.userAgent")
        if native_window:
            await wait_js("!!window.chrome?.webview")
        fixture = os.environ.get("AETHER_SMOKE_SOURCE")
        if fixture:
            if not await client.run_javascript("!!document.querySelector('.game-card')"):
                await wait_js(click("Choose a folder"))
                await wait_js("!!document.querySelector('.q-dialog input')")
                await client.run_javascript(
                    """const el=document.querySelector('.q-dialog input'); el.value="""
                    + json.dumps(fixture)
                    + """; el.dispatchEvent(new Event('input',{bubbles:true}));"""
                )
                await asyncio.sleep(0.4)
                await wait_js(click("Add and scan"))
            await wait_js("!!document.querySelector('.game-card')")
            await client.run_javascript("document.querySelector('.game-card').click()")
            await wait_js("document.body.innerText.includes('Installations & launch profiles')")
            if not await client.run_javascript(
                "!![...document.querySelectorAll('button')].reverse().find(e=>(e.querySelector('.block')?.textContent || e.innerText.trim().split('\\n').pop()).toLowerCase()==='play')"
            ):
                await wait_js(click("Add profile"))
                await wait_js(click("Approve profile"))
            await wait_js(
                "!document.querySelector('.q-dialog__backdrop') && document.body.innerText.includes('Probe.exe')"
            )
            await wait_js(
                "[...document.querySelectorAll('button .block')].some(e => e.textContent === 'Play')"
            )
            outcome["library"] = "scanned and opened a game with an approved profile"
            await wait_js(click("Game folders"))
            await wait_js(click("Manage"))
            await wait_js(
                "[...document.querySelectorAll('.q-dialog .dialog-title')].some(e => e.textContent === 'Manage source')"
            )
            await wait_js(click("Cancel"))
            await wait_js("!document.body.innerText.includes('Manage source')")
            outcome["dialogs"] = "opened and closed source settings"
        await wait_js(click("Settings"))
        await wait_js(
            "document.querySelector('.page-title')?.textContent === 'Settings' && document.body.innerText.includes('RAWG API key')"
        )
        outcome["ok"] = True
    except Exception as exc:
        logging.getLogger(__name__).exception("Packaged UI check failed")
        outcome.update(ok=False, error=str(exc))
    finally:
        result.write_text(json.dumps(outcome), encoding="utf-8")
        if native_window:
            app.native.main_window.destroy()
        else:
            app.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Aether desktop game library")
    parser.add_argument(
        "--browser", action="store_true", help="Open the local interface in your default browser"
    )
    parser.add_argument("--no-open", action="store_true", help="Use browser mode without opening a tab")
    parser.add_argument("--port", type=int, help="Loopback port (default: a free port)")
    parser.add_argument(
        "--smoke-test",
        type=Path,
        metavar="JSON",
        help="Exercise the rendered UI and write a diagnostic result",
    )
    args = parser.parse_args()
    home = Path(os.environ.get("AETHER_DATA_DIR") or user_data_path("Aether", appauthor=False))
    with single_instance(home) as acquired:
        if not acquired:
            if sys.platform == "win32":
                ctypes.windll.user32.MessageBoxW(
                    None, "Aether is already using this library.", "Aether", 0x40
                )
            return
        handler = RotatingFileHandler(
            home / "aether.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        logging.basicConfig(level=logging.WARNING, handlers=[handler])
        db = Database(home)
        monitor, meta, tracker = Monitor(db), Metadata(db), Playtime(db)
        package = Path(__file__).resolve().parent
        version = (package.parent / "VERSION").read_text().strip()
        native_window = not (args.browser or args.no_open)
        if native_window and sys.platform == "win32":
            app.native.start_args["gui"] = "edgechromium"
            app.native.window_args["min_size"] = (720, 490)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
        app.add_middleware(LocalOrigin)
        app.add_static_files("/art", db.art)
        app.add_static_file(url_path="/brand/mark.png", local_file=package / "assets/mark.png")
        ui.add_css(package / "assets/aether.css", shared=True)
        for service in (monitor, meta, tracker):
            app.on_startup(service.start)
            app.on_shutdown(service.stop)

        @ui.page("/")
        def page():
            Shell(db, monitor, meta, tracker, native_window, version)
            if args.smoke_test:
                ui.timer(1, lambda: smoke(args.smoke_test, native_window), once=True)

        ui.run(
            host="127.0.0.1",
            port=args.port or native.find_open_port(),
            native=native_window,
            window_size=(1280, 820) if native_window else None,
            title="Aether",
            favicon=package / "assets/aether.ico",
            show=not args.no_open,
            reload=False,
            dark=True,
            show_welcome_message=False,
        )
