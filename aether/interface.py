"""The desktop shell. Each connected window owns its navigation and filter state."""

import hashlib
import math
import time
from functools import partial

from nicegui import run, ui

from .dialogs import (
    action,
    artwork_dialog,
    edit_dialog,
    field,
    folder_dialog,
    match_dialog,
    modal,
    profile_dialog,
    title,
    warn,
)


def duration(seconds):
    minutes = int(seconds // 60)
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60:02}m"
    if minutes:
        return f"{minutes}m"
    return f"{int(seconds)}s" if seconds else "0m"


def stamp(value):
    return time.strftime("%b %d, %Y · %H:%M", time.localtime(value)) if value else "Never"


def heading(value):
    return ui.label(value).classes("page-title").props("role=heading aria-level=1")


def picture(file):
    return "/art/" + file


def initials(text):
    return "".join(part[0] for part in text.split()[:2]).upper() or "A"


def poster(g):
    if g["cover"]:
        ui.image(picture(g["cover"])).classes("cover-img").props("fit=cover loading=lazy no-spinner")
    else:
        hue = int(hashlib.sha256(g["detected"].encode()).hexdigest()[:4], 16) % 360
        with ui.element("div").classes("placeholder-cover").style(f"--hue:{hue}"):
            ui.label(initials(g["title"])).classes("monogram")


class Shell:
    def __init__(self, db, monitor, meta, tracker, native, version):
        self.db, self.monitor, self.meta, self.tracker = db, monitor, meta, tracker
        self.native, self.version = native, version
        self.view, self.selection = "library", None
        self.query = self.genre = self.platform = self.tag = ""
        self.sort = "Title A–Z"
        self.layout = db.setting("layout", "Grid")
        self.size = int(db.setting("cover_size", "172"))
        self.page = 1
        self.rev = db.revision
        self.theme = ui.dark_mode(
            {"Dark": True, "Light": False, "System": None}.get(db.setting("theme", "Dark"), True)
        )
        self.drawer = (
            ui.left_drawer(value=None).props("show-if-above :breakpoint=850 :width=244").classes("rail")
        )
        with self.drawer:
            with ui.row().classes("brand"):
                ui.image("/brand/mark.png").classes("brand-mark").props("no-spinner")
                ui.label("Aether").classes("brand-name")
            self.navigation = ui.column().classes("nav-stack")
            with ui.column().classes("rail-bottom"):
                self.status_label = ui.label("Library ready").classes("live-status")
                ui.label(f"Aether {version}").classes("version")
        self.top = ui.row().classes("topbar")
        with self.top:
            ui.button(icon="menu", on_click=self.drawer.toggle).props(
                "flat round dense aria-label=Menu"
            ).classes("menu-toggle")
            self.crumb = ui.label("Workspace  /  Library").classes("crumb")
            ui.space()
            action("Add folder", "create_new_folder", lambda: folder_dialog(self), quiet=True)
        self.main = ui.column().classes("main-pane")
        self.go("library")
        ui.timer(0.8, self.tick)

    def nav(self):
        self.navigation.clear()
        with self.navigation:
            for text, icon, view in [
                ("All games", "grid_view", "library"),
                ("Favorites", "star_outline", "favorites"),
                ("Recently played", "history", "recent"),
                ("Collections", "bookmarks", "collections"),
                ("Discovery", "travel_explore", "discovery"),
                ("Game folders", "folder_open", "folders"),
                ("Play activity", "query_stats", "activity"),
                ("Settings", "tune", "settings"),
            ]:
                current = (
                    self.view == view
                    or (view == "collections" and self.view == "collection")
                    or (view == "library" and self.view == "game")
                )
                ui.button(text, icon=icon, color=None, on_click=partial(self.go, view)).props(
                    "flat no-caps align=left"
                ).classes("nav-item active" if current else "nav-item")

    def go(self, view, selection=None):
        self.view, self.selection, self.page = view, selection, 1
        self.query = self.genre = self.platform = self.tag = ""
        self.nav()
        self.main.clear()
        names = {
            "library": "Library",
            "favorites": "Favorites",
            "recent": "Recently played",
            "collection": str(selection),
            "folders": "Game folders",
            "activity": "Play activity",
            "game": "Game details",
        }
        self.crumb.text = "Workspace   /   " + names.get(view, view.title())
        with self.main:
            if view in {"library", "favorites", "recent", "collection"}:
                heading(
                    {"library": "Game library", "favorites": "Favorites", "recent": "Recently played"}.get(
                        view, str(selection)
                    )
                )
                self.toolbar()
            elif view == "game":
                action("Back to library", "arrow_back", partial(self.go, "library"), quiet=True)
            else:
                heading(names.get(view, view.title()))
            self.content = ui.column().classes("view-content")
        self.update()

    def toolbar(self):
        def search(e):
            self.query = e.value or ""
            self.page = 1
            self.update()

        with ui.row().classes("toolbar"):
            with (
                ui.input(placeholder="Search your library", on_change=search)
                .props('outlined dense clearable debounce=180 aria-label="Search games"')
                .classes("search") as box,
                box.add_slot("prepend"),
            ):
                ui.icon("search")
            action("Filters", "filter_list", self.filters, quiet=True)
            self.view_button = (
                ui.button(
                    icon="grid_view" if self.layout == "List" else "view_list", on_click=self.toggle_layout
                )
                .props('flat round aria-label="Change library view"')
                .tooltip("Change grid / list view")
            )

    def toggle_layout(self):
        self.layout = "List" if self.layout == "Grid" else "Grid"
        self.db.set_setting("layout", self.layout)
        self.view_button.props("icon=grid_view" if self.layout == "List" else "icon=view_list")
        self.update()

    def filters(self):
        games = self.db.library()
        dialog, card = modal()
        with card:
            title("Library filters")
            controls = []
            for name, attr, key in [
                ("Genre", "genre", "genres"),
                ("Platform", "platform", "platforms"),
                ("Tag", "tag", "tags"),
            ]:
                vals = sorted({x for g in games for x in g[key]}, key=str.casefold)
                value = getattr(self, attr)
                controls.append(
                    (
                        attr,
                        field(
                            ui.select(
                                ["", *vals], label=name, value=value if value in vals else "", clearable=True
                            )
                        ),
                    )
                )
            order = field(
                ui.select(
                    ["Title A–Z", "Last played", "Most played", "Release date", "Rating"],
                    label="Sort by",
                    value=self.sort,
                )
            )
            ui.label("Cover width")
            size = ui.slider(min=130, max=260, step=2, value=self.size).props("label")

            def apply():
                for attr, control in controls:
                    setattr(self, attr, control.value or "")
                self.sort, self.size, self.page = order.value, int(size.value), 1
                self.db.set_setting("cover_size", str(self.size))
                dialog.close()
                self.update()

            action("Apply filters", "check", apply)
        dialog.open()

    def tick(self):
        self.status_label.text = "Scanning folders…" if self.monitor.activity else "Library ready"
        if self.rev != self.db.revision:
            self.rev = self.db.revision
            # Settings contains editable controls. Preserve in-progress edits during background scans.
            if self.view != "settings":
                self.update()

    def update(self):
        self.rev = self.db.revision
        self.content.clear()
        with self.content:
            if self.view in {"library", "favorites", "recent", "collection"}:
                self.library()
            else:
                getattr(self, "show_" + self.view)()

    def game_menu(self, g):
        def fav():
            self.db.favorite(g["id"])
            self.update()

        with ui.context_menu():
            ui.menu_item("Open game", on_click=partial(self.go, "game", g["id"]))
            ui.menu_item("Remove favorite" if g["favorite"] else "Add favorite", on_click=fav)
            ui.menu_item("Edit game", on_click=lambda: edit_dialog(self, g))
            ui.menu_item("Match with RAWG", on_click=lambda: match_dialog(self, g))

    def library(self):
        games = self.db.library()
        if not games and self.view == "library" and not self.query:
            with ui.element("div").classes("first-run"):
                with ui.element("div").classes("shelf-art").props("aria-hidden=true"):
                    for klass in ["art-back", "art-mid", "art-front"]:
                        with ui.element("div").classes("art-tile " + klass):
                            if klass == "art-front":
                                ui.image("/brand/mark.png").props("no-spinner").classes("shelf-mark")
                ui.label("Add your first game folder").classes("empty-title")
                ui.label(
                    "Choose a folder containing installed games. Aether will find their executables and keep your library in sync."
                ).classes("empty-copy")
                action("Choose a folder", "folder_open", lambda: folder_dialog(self))
            return
        q = self.query.casefold().strip()
        games = [
            g
            for g in games
            if (self.view != "favorites" or g["favorite"])
            and (self.view != "recent" or g["last_played"])
            and (self.view != "collection" or self.selection in g["collections"])
            and (not self.genre or self.genre in g["genres"])
            and (not self.platform or self.platform in g["platforms"])
            and (not self.tag or self.tag in g["tags"])
            and (not q or q in " ".join([g["title"], g["developer"], *g["tags"], *g["genres"]]).casefold())
        ]
        key = {
            "Last played": "last_played",
            "Most played": "seconds",
            "Release date": "released",
            "Rating": "rating",
        }.get(self.sort)
        if self.view == "recent" and self.sort == "Title A–Z":
            key = "last_played"
        games.sort(key=lambda g: g["title"].casefold())
        if key:
            games.sort(key=lambda g: g[key] or ("" if key == "released" else 0), reverse=True)
        with ui.row().classes("list-meta"):
            ui.label(f"{len(games)} game{'s' if len(games) != 1 else ''}")
            for v in filter(None, [self.genre, self.platform, self.tag]):
                ui.badge(v).props("outline")
        if not games:
            self.empty(
                "No games here yet" if not q else "No matching games",
                "Adjust your search or filters."
                if q or self.genre or self.tag or self.platform
                else "Games appear here as you organize and play them.",
            )
            return
        limit = 36
        pages = max(1, math.ceil(len(games) / limit))
        self.page = min(self.page, pages)
        page = games[(self.page - 1) * limit : self.page * limit]
        if self.layout == "Grid":
            with ui.element("div").classes("game-grid").style(f"--card-width:{self.size}px"):
                for g in page:
                    open_game = partial(self.go, "game", g["id"])
                    with (
                        ui.element("article")
                        .classes("game-card")
                        .props("tabindex=0 role=button")
                        .on("click", open_game)
                        .on("keydown.enter", open_game)
                    ):
                        with ui.element("div").classes("cover"):
                            poster(g)
                            if g["favorite"]:
                                ui.icon("star").classes("fav-mark")
                        ui.label(g["title"]).classes("game-name")
                        ui.label(self.caption(g)).classes("game-caption")
                        self.game_menu(g)
        else:
            for g in page:
                with (
                    ui.row()
                    .classes("list-entry")
                    .props("tabindex=0 role=button")
                    .on("click", partial(self.go, "game", g["id"]))
                    .on("keydown.enter", partial(self.go, "game", g["id"]))
                ):
                    with ui.element("div").classes("mini-cover"):
                        poster(g)
                    with ui.column().classes("gap-0 grow"):
                        ui.label(g["title"]).classes("entry-title")
                        ui.label(self.caption(g)).classes("muted small")
                    ui.label(duration(g["seconds"])).classes("muted")
                    ui.icon("chevron_right")
                    self.game_menu(g)
        if pages > 1:

            def select_page(e):
                self.page = e.value
                self.update()

            ui.pagination(1, pages, value=self.page, on_change=select_page).props("max-pages=7")
        if any(g["rawg_id"] for g in page):
            ui.link("Game data and artwork by RAWG", "https://rawg.io", new_tab=True).classes("credit")

    @staticmethod
    def caption(g):
        if g["playing"]:
            return "Playing now"
        state = "Installed" if g["installed"] else "Unavailable"
        return " · ".join([state, (g["genres"] or ["PC"])[0]])

    @staticmethod
    def empty(label, detail):
        with ui.column().classes("empty-panel"):
            ui.label(label).classes("section-title")
            ui.label(detail).classes("muted")

    def show_game(self):
        g = self.db.game(self.selection)
        if not g:
            self.empty("Game identity updated", "Open the game from your library.")
            return
        art = self.db.gallery(g["id"])
        background = next((a["file"] for a in art if a["kind"] == "background"), None)
        logo = next((a["file"] for a in art if a["kind"] == "logo"), None)
        installs = self.db.installations(g["id"])
        with ui.element("div").classes("hero-game") as hero:
            if background:
                hero.style(
                    f'background-image:linear-gradient(90deg,#111c2aee,#111c2a99),url("{picture(background)}")'
                )
            with ui.element("div").classes("detail-poster"):
                poster(g)
            with ui.column().classes("hero-copy"):
                if logo:
                    ui.image(picture(logo)).classes("game-logo").props("fit=contain")
                heading(g["title"])
                ui.label(" · ".join(filter(None, [self.caption(g), g["released"], g["developer"]]))).classes(
                    "hero-meta"
                )
                with ui.row().classes("gap-2"):
                    action(
                        "Favorite" if not g["favorite"] else "Favorited",
                        "star" if g["favorite"] else "star_border",
                        lambda: (self.db.favorite(g["id"]), self.update()),
                        quiet=True,
                    )
                    action("Edit game", "edit", lambda: edit_dialog(self, g), quiet=True)
                    action("Match with RAWG", "manage_search", lambda: match_dialog(self, g), quiet=True)
        with ui.row().classes("metrics"):
            for name, val in [
                ("Playtime", duration(g["seconds"])),
                ("Last played", stamp(g["last_played"])),
                ("Installations", str(g["copies"])),
                ("Rating", f"{g['rating']:.1f} / 5" if g["rating"] is not None else "—"),
            ]:
                with ui.column().classes("metric"):
                    ui.label(name).classes("muted small")
                    ui.label(val).classes("metric-value")
        for name, value in [
            ("About", g["description"]),
            ("Platforms", ", ".join(g["platforms"])),
            ("Publisher", g["publisher"]),
            ("Tags", ", ".join(g["tags"])),
            ("Collections", ", ".join(g["collections"])),
            ("Notes", g["notes"]),
        ]:
            if value:
                ui.label(name).classes("section-title")
                ui.label(value).classes("body-copy")
        ui.label("Installations & launch profiles").classes("section-title")
        for inst in installs:
            state = (
                inst["source_status"]
                if not inst["enabled"] or inst["source_status"] == "Offline"
                else inst["status"]
            )
            with ui.column().classes("panel"):
                with ui.row().classes("rowline"):
                    with ui.column().classes("grow gap-1"):
                        ui.label(inst["path"]).classes("path")
                        ui.label(" · ".join(filter(None, [state, inst["version"]]))).classes("muted small")
                    action("Add profile", "add", lambda inst=inst: profile_dialog(self, inst), quiet=True)
                profiles = self.db.profiles(inst["id"])
                if not profiles:
                    ui.label("Approve an executable to play this installation.").classes("muted")
                for p in profiles:
                    with ui.row().classes("rowline profile-row"):
                        with ui.column().classes("grow gap-0"):
                            ui.label(p["name"]).classes("entry-title")
                            ui.label(p["executable"]).classes("path small")

                        async def play(pid=p["id"]):
                            try:
                                await run.io_bound(self.tracker.launch, pid)
                                ui.notify("Game launched.")
                                self.update()
                            except (ValueError, OSError) as exc:
                                warn(exc)

                        play_button = action("Play", "play_arrow", play)
                        if state in {"Offline", "Missing", "Ignored", "Paused", "Removed"} or g["playing"]:
                            play_button.disable()
                        action(
                            "Edit", "tune", lambda inst=inst, p=p: profile_dialog(self, inst, p), quiet=True
                        )
                        action(
                            "Revoke",
                            "remove_circle_outline",
                            lambda p=p: self.confirm(
                                "Revoke this launch profile?", lambda: self.db.revoke(p["id"])
                            ),
                            quiet=True,
                        )
        with ui.row().classes("rowline"):
            ui.label("Artwork").classes("section-title grow")
            action("Add artwork", "add_photo_alternate", lambda: artwork_dialog(self, g["id"]), quiet=True)
        if art:
            with ui.element("div").classes("gallery"):
                for a in art:
                    with ui.column().classes("gap-1"):
                        ui.image(picture(a["file"])).classes("gallery-img").props("fit=contain").on(
                            "click", lambda a=a: self.lightbox(a)
                        )
                        ui.label(f"{a['kind'].title()} · {a['source']}").classes("muted small")
        if g["rawg_id"]:
            slug = g.get("rawg_slug") or str(g["rawg_id"])
            ui.link("Game data and artwork by RAWG", "https://rawg.io/games/" + slug, new_tab=True).classes(
                "credit"
            )

    @staticmethod
    def lightbox(a):
        d, c = modal()
        with c.classes("lightbox"):
            ui.image(picture(a["file"])).props("fit=contain").classes("w-full")
            action("Close", "close", d.close, quiet=True)
        d.open()

    def confirm(self, message, callback):
        d, c = modal()
        with c:
            title(message)

            def accept():
                callback()
                d.close()
                self.update()

            with ui.row().classes("modal-actions"):
                action("Cancel", None, d.close, quiet=True)
                action("Confirm", "check", accept)
        d.open()

    def show_collections(self):
        def create():
            d, c = modal()
            with c:
                title("New collection")
                name = field(ui.input("Collection name"))

                def save():
                    try:
                        self.db.create_group(name.value)
                        d.close()
                        self.update()
                    except ValueError as e:
                        warn(e)

                action("Create collection", "add", save)
            d.open()

        action("New collection", "add", create)
        groups = self.db.groups()
        if not groups:
            self.empty("Organize your games", "Create a collection, then add games through Edit game.")
        for group in groups:
            with ui.row().classes("panel rowline"):
                ui.icon("bookmarks").classes("tile-icon")
                with ui.column().classes("grow gap-0"):
                    ui.label(group["name"]).classes("entry-title")
                    ui.label(f"{group['amount']} games").classes("muted")
                action("Open", "arrow_forward", partial(self.go, "collection", group["name"]), quiet=True)
                action(
                    "Delete",
                    "close",
                    lambda group=group: self.confirm(
                        "Delete this collection?", lambda: self.db.delete_group(group["id"])
                    ),
                    quiet=True,
                )

    def show_discovery(self):
        pending = [g for g in self.db.library() if not g["rawg_id"] and g["match_state"] != "Local"]
        local = [g for g in self.db.library() if g["match_state"] == "Local"]
        if not pending:
            self.empty("Discovery inbox is clear", "New installations appear here for metadata review.")

        def line(g, is_local):
            with ui.row().classes("panel rowline"):
                with ui.column().classes("grow gap-0"):
                    ui.label(g["title"]).classes("entry-title")
                    ui.label(
                        {
                            "New": "Awaiting match",
                            "Retry": "RAWG unavailable · retry pending",
                            "Review": "Choose a match",
                        }.get(g["match_state"], "Local metadata")
                    ).classes("muted small")
                action("Match", "manage_search", lambda: match_dialog(self, g))
                if not is_local:
                    action(
                        "Keep local",
                        "offline_pin",
                        lambda: (self.meta.set_state(g["id"], "Local"), self.update()),
                        quiet=True,
                    )

        for g in pending:
            line(g, False)
        if local:
            with ui.expansion(f"Kept local ({len(local)})").classes("w-full"):
                for g in local:
                    line(g, True)

    def show_folders(self):
        with ui.row().classes("gap-2"):
            action("Add source", "create_new_folder", lambda: folder_dialog(self))
            action(
                "Rescan all",
                "refresh",
                lambda: (self.monitor.rescan(), ui.notify("Scan queued.")),
                quiet=True,
            )
        folders = self.db.folders()
        if not folders:
            self.empty(
                "No monitored folders",
                "Add a library folder or a single installation to start discovering games.",
            )
        if self.monitor.activity:
            ui.linear_progress().props("indeterminate").classes("w-full")
        for f in folders:
            with ui.row().classes("panel rowline"):
                ui.icon("folder_open").classes("tile-icon")
                with ui.column().classes("grow gap-0"):
                    ui.label(f["path"]).classes("path")
                    ui.label(
                        f"{f['status']} · {'Library folder' if f['mode'] == 'library' else 'Single installation'} · Last scan {stamp(f['scanned'])}"
                    ).classes("muted small")
                action("Manage", "tune", lambda f=f: folder_dialog(self, f), quiet=True)
                action(
                    "Remove",
                    "remove_circle_outline",
                    lambda f=f: self.confirm(
                        "Stop monitoring and remove this source? Your games and history will be kept.",
                        lambda: self.db.remove_folder(f["id"]),
                    ),
                    quiet=True,
                )
        logs = self.db.rows(
            "SELECT s.*,f.path FROM scans s JOIN folders f ON f.id=s.folder ORDER BY s.id DESC LIMIT 12"
        )
        if logs:
            ui.label("Scan history").classes("section-title")
            for row in logs:
                with ui.column().classes("log-entry"):
                    ui.label(row["message"]).classes("log-message")
                    ui.label(f"{stamp(row['time'])} · {row['path']}").classes("muted small")

    def show_activity(self):
        games = sorted(self.db.library(), key=lambda g: -g["seconds"])
        sessions = self.db.rows("""SELECT s.*,i.game FROM sessions s JOIN installations i ON i.id=s.installation
                                   ORDER BY s.start DESC LIMIT 100""")
        names = {g["id"]: g["title"] for g in games}
        total = sum(g["seconds"] for g in games)
        ui.label(f"{duration(total)} played across your library").classes("section-title")
        if not sessions:
            self.empty("No play sessions yet", "Launch an approved game to record your first session.")
            return
        ranked = [g for g in games if g["seconds"] > 0][:5]
        if ranked:
            with ui.column().classes("panel"):
                ui.label("Most played").classes("section-title")
                for g in ranked:
                    with ui.row().classes("rowline"):
                        ui.label(g["title"]).classes("grow")
                        ui.label(duration(g["seconds"])).classes("muted")
                    ui.linear_progress(value=g["seconds"] / ranked[0]["seconds"]).classes("w-full")
        ui.label("Recent sessions").classes("section-title")
        for s in sessions:
            with ui.row().classes("panel rowline"):
                with ui.column().classes("grow gap-0"):
                    ui.label(names[s["game"]]).classes("entry-title")
                    ui.label(stamp(s["start"])).classes("muted small")
                ui.label(
                    "Playing now" if s["end"] is None else duration(max(0, s["end"] - s["start"]))
                ).classes("muted")

    def show_settings(self):
        with ui.column().classes("panel settings"):
            ui.label("Appearance").classes("section-title")

            def theme(e):
                self.db.set_setting("theme", e.value)
                self.theme.value = {"Light": False, "Dark": True, "System": None}[e.value]

            field(
                ui.select(
                    ["Dark", "Light", "System"],
                    label="Color theme",
                    value=self.db.setting("theme", "Dark"),
                    on_change=theme,
                )
            )
        with ui.column().classes("panel settings"):
            ui.label("RAWG metadata").classes("section-title")
            ui.label("Connect your own RAWG account to look up titles, descriptions and artwork.").classes(
                "muted"
            )
            status = ui.label(
                "API key configured" if self.meta.credentials.key else "No API key configured"
            ).classes("small")
            api = field(ui.input("RAWG API key", password=True, password_toggle_button=True)).props(
                "autocomplete=off"
            )

            async def save_key(clear=False):
                if not clear and not api.value.strip():
                    return warn("Enter an API key.")
                try:
                    await run.io_bound(self.meta.credentials.save, "" if clear else api.value)
                    api.value = ""
                    status.text = (
                        "API key configured" if self.meta.credentials.key else "No API key configured"
                    )
                    with self.db.connection(True) as db:
                        db.execute("UPDATE games SET match_state='New' WHERE match_state='Retry'")
                    self.meta.wake.set()
                    ui.notify("Credential updated.")
                except ValueError as e:
                    warn(e)

            with ui.row().classes("gap-2"):
                action("Save key", "key", lambda: save_key())
                action("Clear saved key", "delete_outline", lambda: save_key(True), quiet=True)
            ui.link("Get a RAWG API key", "https://rawg.io/apidocs", new_tab=True).classes("credit")

            def automatic(e):
                self.db.set_setting("automatch", "on" if e.value else "off")
                self.meta.wake.set()

            ui.switch(
                "Automatically match unambiguous PC titles",
                value=self.db.setting("automatch", "on") == "on",
                on_change=automatic,
            )
        with ui.column().classes("panel settings"):
            ui.label("Playtime").classes("section-title")
            ui.switch(
                "Track approved games launched outside Aether while it is open",
                value=self.db.setting("external", "on") == "on",
                on_change=lambda e: self.db.set_setting("external", "on" if e.value else "off"),
            )
        with ui.column().classes("panel settings"):
            ui.label("Local data").classes("section-title")
            ui.label(str(self.db.home)).classes("path")
            ui.label(f"Aether {self.version} · SQLite library and offline artwork").classes("muted small")
