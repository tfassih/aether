import inspect
import json
from pathlib import Path

from nicegui import app, run, ui

from .metadata import MAX_IMAGE, save_image


def action(text, icon, handler, *, quiet=False):
    client = ui.context.client

    async def invoke():
        # Refreshing the game grid can delete the button while its asynchronous action is running.
        with client:
            result = handler()
            if inspect.isawaitable(result):
                await result

    return (
        ui.button(text, icon=icon, color=None, on_click=invoke)
        .props("no-caps flat" if quiet else "no-caps unelevated")
        .classes("action secondary" if quiet else "action")
    )


def field(control):
    return control.props("outlined dense").classes("w-full")


def title(text):
    return ui.label(text).classes("dialog-title")


def modal():
    with ui.context.client:
        dialog = ui.dialog()
    with dialog:
        card = ui.card().classes("modal-card")
    # Dialogs are single-use. Release their controls and event listeners after the exit animation.
    dialog.on("hide", dialog.delete)
    return dialog, card


def warn(exc):
    ui.notify(str(exc), type="warning", multi_line=True, close_button=True)


async def choose_folder(target):
    import webview

    result = await app.native.main_window.create_file_dialog(webview.FileDialog.FOLDER)
    if result:
        target.value = result[0]


def folder_dialog(shell, folder=None):
    dialog, card = modal()
    with card:
        title("Manage source" if folder else "Add a game folder")
        if folder:
            ui.label(folder["path"]).classes("path")
        else:
            with ui.row().classes("rowline"):
                path = field(ui.input("Folder path", placeholder=r"D:\Games"))
                if shell.native:
                    action("Browse", "folder_open", lambda: choose_folder(path), quiet=True)
            mode = field(
                ui.select(
                    {
                        "library": "Library folder — one game per subfolder",
                        "single": "Single game installation",
                    },
                    label="Folder contents",
                    value="library",
                )
            )
        depth = field(
            ui.number(
                "Search depth inside each game", value=folder["depth"] if folder else 6, min=0, max=16, step=1
            )
        )
        if folder:
            enabled = ui.switch("Monitor this folder", value=bool(folder["enabled"]))
            ignored = field(
                ui.textarea(
                    "Excluded paths · one relative path per line",
                    value="\n".join(shell.db.exclusions(folder["id"])),
                )
            )

        async def save():
            try:
                if folder:
                    await run.io_bound(
                        shell.db.configure_folder,
                        folder["id"],
                        enabled.value,
                        int(depth.value),
                        ignored.value.splitlines(),
                    )
                    fid = folder["id"]
                else:
                    fid = await run.io_bound(shell.db.add_folder, path.value, mode.value, int(depth.value))
                shell.monitor.queue(fid, immediate=True)
                dialog.close()
                shell.update()
                ui.notify("Source updated" if folder else "Folder added. Scanning for games.")
            except (ValueError, TypeError, OSError) as exc:
                warn(exc)

        with ui.row().classes("modal-actions"):
            action("Cancel", None, dialog.close, quiet=True)
            action("Save changes" if folder else "Add and scan", "check" if folder else "add", save)
    dialog.open()


def edit_dialog(shell, g):
    dialog, card = modal()
    with card:
        title("Edit game")
        name = field(ui.input("Game title", value=g["title"]))
        desc = field(ui.textarea("Description", value=g["description"]))
        with ui.row().classes("rowline"):
            released = field(ui.input("Release date", value=g["released"], placeholder="YYYY-MM-DD"))
            rating = field(ui.number("Rating / 5", value=g["rating"], min=0, max=5, step=0.1))
        genres = field(ui.input("Genres · comma separated", value=", ".join(g["genres"])))
        platforms = field(ui.input("Platforms · comma separated", value=", ".join(g["platforms"])))
        dev = field(ui.input("Developer", value=g["developer"]))
        pub = field(ui.input("Publisher", value=g["publisher"]))
        tags = field(ui.input("Tags · comma separated", value=", ".join(g["tags"])))
        groups = field(
            ui.select(
                [r["name"] for r in shell.db.groups()],
                label="Collections",
                value=g["collections"],
                multiple=True,
                with_input=True,
                new_value_mode="add-unique",
            )
        ).props("use-chips")
        notes = field(ui.textarea("Personal notes", value=g["notes"]))

        def save():
            from datetime import date

            if not name.value.strip():
                return warn("Enter a title.")
            if released.value.strip():
                try:
                    date.fromisoformat(released.value.strip())
                except ValueError:
                    return warn("Use YYYY-MM-DD for the release date.")
            if rating.value is not None and not 0 <= rating.value <= 5:
                return warn("Choose a rating between 0 and 5.")

            def items(v):
                return [x.strip() for x in v.split(",") if x.strip()]

            values = {
                "title": name.value.strip(),
                "description": desc.value.strip(),
                "released": released.value.strip(),
                "rating": rating.value,
                "genres": items(genres.value),
                "platforms": items(platforms.value),
                "notes": notes.value.strip(),
                "developer": dev.value.strip(),
                "publisher": pub.value.strip(),
            }
            edits = json.loads(g["edits"])
            for key, value in values.items():
                if value != g[key]:
                    edits[key] = value
            shell.db.edit_game(g["id"], edits, items(tags.value), groups.value)
            dialog.close()
            shell.update()

        with ui.row().classes("modal-actions"):
            action("Cancel", None, dialog.close, quiet=True)
            action("Save changes", "check", save)
    dialog.open()


def profile_dialog(shell, inst, profile=None):
    dialog, card = modal()
    with card:
        title("Edit launch profile" if profile else "Approve a launch profile")
        ui.label(inst["path"]).classes("path")
        name = field(ui.input("Profile name", value=profile["name"] if profile else "Default"))
        if profile:
            name.props("readonly")
        candidates = inst["candidates"]
        exe = field(
            ui.input(
                "Executable relative to installation",
                value=profile["executable"] if profile else (candidates[0] if candidates else ""),
            )
        )
        if candidates:
            field(
                ui.select(
                    candidates, label="Discovered candidates", on_change=lambda e: exe.set_value(e.value)
                )
            )
        if shell.native:

            async def browse():
                import webview

                result = await app.native.main_window.create_file_dialog(
                    webview.FileDialog.OPEN, directory=inst["path"], file_types=("Executables (*.exe)",)
                )
                if result:
                    try:
                        exe.value = Path(result[0]).relative_to(inst["path"]).as_posix()
                    except ValueError:
                        warn("Choose an executable within this installation.")

            action("Browse for executable", "folder_open", browse, quiet=True)
        cwd = field(
            ui.input(
                "Working directory relative to installation",
                value=profile["cwd"] if profile else (Path(exe.value).parent.as_posix() or "."),
            )
        )
        args = field(
            ui.textarea(
                "Arguments · one argument per line", value="\n".join(profile["arguments"]) if profile else ""
            )
        )
        ui.label("Approving this file enables the Play button for this profile.").classes("muted")

        def save():
            try:
                shell.db.approve(inst["id"], name.value, exe.value, args.value.splitlines(), cwd.value)
                dialog.close()
                shell.update()
                ui.notify("Launch profile approved.")
            except (ValueError, OSError) as exc:
                warn(exc)

        with ui.row().classes("modal-actions"):
            action("Cancel", None, dialog.close, quiet=True)
            action("Approve profile", "verified", save)
    dialog.open()


def artwork_dialog(shell, gid):
    dialog, card = modal()
    with card:
        title("Add artwork")
        kind = field(
            ui.select(["cover", "background", "logo", "screenshot"], label="Artwork type", value="cover")
        )

        async def upload(e):
            try:
                name = await run.io_bound(save_image, await e.file.read(), shell.db.art)
                shell.db.add_art(gid, kind.value, name)
                dialog.close()
                shell.update()
            except (ValueError, OSError) as exc:
                warn(exc)

        ui.upload(
            label="Choose an image · up to 18 MB",
            on_upload=upload,
            on_rejected=lambda: warn("Choose an image smaller than 18 MB."),
            max_file_size=MAX_IMAGE,
            auto_upload=True,
        ).props('accept="image/*" flat bordered').classes("w-full")
        action("Close", None, dialog.close, quiet=True)
    dialog.open()


def match_dialog(shell, g):
    dialog, card = modal()
    with card:
        title("Match with RAWG")
        query = field(ui.input("Search title", value=g["title"]))
        busy = ui.linear_progress().props("indeterminate").classes("w-full")
        busy.visible = False
        results = ui.column().classes("w-full gap-2")

        async def apply(hit):
            busy.visible = True
            try:
                gid, missed = await run.io_bound(shell.meta.match, g["id"], hit["id"])
                dialog.close()
                shell.go("game", gid)
                ui.notify(
                    "Metadata saved. Some artwork is unavailable."
                    if missed
                    else "Metadata and available artwork saved."
                )
            except (OSError, ValueError) as exc:
                warn(exc)
            finally:
                if not busy.is_deleted:
                    busy.visible = False

        async def search():
            if not query.value.strip():
                return
            busy.visible = True
            results.clear()
            try:
                hits = await run.io_bound(shell.meta.search, query.value)
                if results.is_deleted:
                    return
                with results:
                    if not hits:
                        ui.label("No matches. Try another spelling or a shorter title.").classes("muted")
                    for hit in hits:
                        with ui.row().classes("match-line"):
                            with ui.column().classes("gap-0 grow"):
                                ui.label(hit["title"]).classes("entry-title")
                                ui.label(
                                    " · ".join(
                                        filter(
                                            None,
                                            [
                                                hit["year"],
                                                ", ".join(hit["platforms"][:3]),
                                                f"{hit['score']}% match",
                                            ],
                                        )
                                    )
                                ).classes("muted small")
                            action("Use match", "check", lambda hit=hit: apply(hit), quiet=True)
            except (OSError, ValueError) as exc:
                warn(exc)
            finally:
                if not busy.is_deleted:
                    busy.visible = False

        query.on("keydown.enter", search)
        with ui.row().classes("modal-actions"):
            action("Close", None, dialog.close, quiet=True)
            action("Search RAWG", "search", search)
        ui.link("Game data by RAWG", "https://rawg.io", new_tab=True).classes("credit")
    dialog.open()
