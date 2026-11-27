"""RAWG metadata, reviewable title matching and a dedicated offline artwork cache."""

import hashlib
import io
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import keyring
from PIL import Image, ImageOps, UnidentifiedImageError
from rapidfuzz import fuzz

from .discovery import clean_title, norm

log = logging.getLogger(__name__)
MAX_IMAGE = 18 * 1024 * 1024


class MetadataError(ValueError):
    pass


class Credentials:
    def __init__(self):
        self._key = os.environ.get("RAWG_API_KEY", "").strip()
        if not self._key:
            try:
                self._key = self.vault().get_password("Aether", "RAWG") or ""
            except (keyring.errors.KeyringError, ImportError, RuntimeError, OSError):
                self._key = ""

    @staticmethod
    def vault():
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            return WinVaultKeyring()
        return keyring.get_keyring()

    @property
    def key(self):
        return self._key

    def save(self, key):
        key = key.strip()
        try:
            vault = self.vault()
            if key:
                vault.set_password("Aether", "RAWG", key)
            elif vault.get_password("Aether", "RAWG"):
                vault.delete_password("Aether", "RAWG")
        except Exception as exc:
            raise MetadataError(
                "Your operating system credential store is unavailable. You can also set RAWG_API_KEY before opening Aether."
            ) from exc
        self._key = key or os.environ.get("RAWG_API_KEY", "").strip()


def save_image(data: bytes, dest: Path):
    if len(data) > MAX_IMAGE:
        raise MetadataError("Choose an image smaller than 18 MB.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > 36_000_000:
                raise MetadataError("Choose an image below 36 megapixels.")
            frame = ImageOps.exif_transpose(img)
            frame.thumbnail((1920, 1920))
            image = frame.convert(
                "RGBA" if "A" in frame.getbands() or "transparency" in frame.info else "RGB"
            )
            encoded = io.BytesIO()
            image.save(encoded, "WEBP", quality=85, method=4)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise MetadataError("This image could not be read.") from exc
    blob = encoded.getvalue()
    name = hashlib.sha256(blob).hexdigest() + ".webp"
    if not (dest / name).exists():
        with tempfile.NamedTemporaryFile(dir=dest, suffix=".tmp", delete=False) as file:
            tmp = Path(file.name)
            try:
                file.write(blob)
            except BaseException:
                file.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            tmp.replace(dest / name)
        finally:
            tmp.unlink(missing_ok=True)
    return name


def rank(query, hits):
    result = []
    for item in hits:
        if not isinstance(item.get("id"), int) or not item.get("name"):
            continue
        platforms = [
            p["platform"]["name"] for p in item.get("platforms") or [] if p.get("platform", {}).get("name")
        ]
        ratio = fuzz.ratio(norm(query), norm(item["name"]))
        year = (item.get("released") or "")[:4]
        score = min(
            100, round(ratio + (1 if "PC" in platforms else 0) + (2 if year and year in query else 0))
        )
        result.append(
            {"id": item["id"], "title": item["name"], "year": year, "platforms": platforms, "score": score}
        )
    return sorted(result, key=lambda row: (-row["score"], row["title"], row["id"]))


class Metadata:
    def __init__(self, db, credentials=None, transport=None):
        self.db = db
        self.credentials = credentials or Credentials()
        self._http = None
        self.transport = transport
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.retry_after = 0.0

    @property
    def http(self):
        with self.lock:
            if self._http is None:
                self._http = httpx.Client(
                    timeout=httpx.Timeout(10, connect=4),
                    transport=self.transport,
                    headers={"User-Agent": "Aether-Desktop/1.0"},
                )
            return self._http

    def _get(self, endpoint, params=None):
        params = dict(params or {})
        cache_key = f"{endpoint}|{sorted(params.items())}"
        old, saved = self.db.cached(cache_key)
        if old is not None and time.time() - saved < 7 * 86400:
            return old
        if not self.credentials.key:
            if old is not None:
                return old
            raise MetadataError("Add your RAWG API key in Settings to search online.")
        if time.time() < self.retry_after:
            if old is not None:
                return old
            raise MetadataError("RAWG asked Aether to pause requests. Try again later.")
        try:
            r = self.http.get(
                "https://api.rawg.io/api/" + endpoint, params=params | {"key": self.credentials.key}
            )
            if r.status_code in {401, 403}:
                raise MetadataError("RAWG rejected the API key. Check the key in Settings.")
            if r.status_code == 429:
                delay = r.headers.get("Retry-After", "60")
                self.retry_after = time.time() + (min(int(delay), 86400) if delay.isdecimal() else 60)
                raise MetadataError("RAWG’s request limit was reached. Aether will wait before retrying.")
            r.raise_for_status()
            value = r.json()
            if not isinstance(value, dict):
                raise MetadataError("RAWG returned an unreadable response.")
        except (httpx.HTTPError, ValueError) as exc:
            if old is not None:
                return old
            if isinstance(exc, MetadataError):
                raise
            raise MetadataError("RAWG is unavailable. Your local library is still accessible.") from None
        self.db.cache(cache_key, value)
        return value

    def search(self, query):
        query = clean_title(query.strip())
        return rank(query, self._get("games", {"search": query, "page_size": 12}).get("results") or [])

    def image(self, url):
        host = urlsplit(url)
        if host.scheme != "https" or host.hostname not in {
            "media.rawg.io",
            "images.rawg.io",
            "resize.rawg.io",
        }:
            raise MetadataError("RAWG returned an unsupported artwork address.")
        key = "image|" + url
        old, _ = self.db.cached(key)
        if old and (self.db.art / old).is_file():
            return old
        data = bytearray()
        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(65536):
                data.extend(chunk)
                if len(data) > MAX_IMAGE:
                    raise MetadataError("RAWG artwork exceeded the size limit.")
        file = save_image(bytes(data), self.db.art)
        self.db.cache(key, file)
        return file

    def match(self, gid, rawg_id, *, automatic=False):
        with self.lock:
            if automatic:
                row = self.db.rows("SELECT rawg_id,match_state FROM games WHERE id=?", (gid,))
                if not row or row[0]["rawg_id"] or row[0]["match_state"] == "Local":
                    return gid, 0
            info = self._get(f"games/{int(rawg_id)}")
            if not info.get("name") or info.get("id") != rawg_id:
                raise MetadataError("RAWG returned an incomplete game record.")

            def names(values):
                return [v["name"] for v in values or [] if v.get("name")]

            slug = info.get("slug", "")
            data = {
                "title": info["name"],
                "description": info.get("description_raw") or "",
                "released": info.get("released") or "",
                "rating": info.get("rating"),
                "genres": names(info.get("genres")),
                "developer": ", ".join(names(info.get("developers"))),
                "publisher": ", ".join(names(info.get("publishers"))),
                "platforms": names([v.get("platform") or {} for v in info.get("platforms") or []]),
                "rawg_slug": slug if re.fullmatch(r"[a-zA-Z0-9_-]+", slug) else "",
            }
            gid = self.db.apply_match(gid, rawg_id, data)
            urls = [("background", info["background_image"])] if info.get("background_image") else []
            missing = 0
            try:
                shots = self._get(f"games/{rawg_id}/screenshots", {"page_size": 6})
                urls.extend(
                    ("screenshot", s["image"]) for s in (shots.get("results") or [])[:6] if s.get("image")
                )
            except MetadataError:
                missing += 1
            for kind, url in urls:
                if self.stop_event.is_set():
                    break
                try:
                    self.db.add_art(gid, kind, self.image(url), "RAWG")
                except (OSError, httpx.HTTPError, ValueError):
                    missing += 1
            return gid, missing

    def set_state(self, gid, state):
        with self.db.connection(True) as db:
            db.execute(
                "UPDATE games SET match_state=?,attempted=? WHERE id=? AND rawg_id IS NULL",
                (state, time.time(), gid),
            )

    def start(self):
        self.thread = threading.Thread(target=self._work, name="aether-metadata", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(3)
        if self._http is not None and (not self.thread or not self.thread.is_alive()):
            self._http.close()

    def _work(self):
        while not self.stop_event.is_set():
            wait = 4
            try:
                if (
                    self.credentials.key
                    and self.db.setting("automatch", "on") == "on"
                    and time.time() >= self.retry_after
                ):
                    rows = self.db.rows(
                        """SELECT id,detected FROM games WHERE rawg_id IS NULL AND
                        (match_state='New' OR (match_state='Retry' AND attempted<?))
                        AND EXISTS(SELECT 1 FROM installations i JOIN folders f ON f.id=i.folder
                            WHERE i.game=games.id AND f.enabled AND f.status!='Offline' AND i.status='Installed')
                        ORDER BY id LIMIT 1""",
                        (time.time() - 1800,),
                    )
                    if rows:
                        item = rows[0]
                        try:
                            hits = self.search(item["detected"])
                            if (
                                hits
                                and norm(hits[0]["title"]) == norm(item["detected"])
                                and "PC" in hits[0]["platforms"]
                                and (len(hits) == 1 or hits[0]["score"] - hits[1]["score"] >= 8)
                            ):
                                self.match(item["id"], hits[0]["id"], automatic=True)
                            else:
                                self.set_state(item["id"], "Review")
                        except MetadataError:
                            self.set_state(item["id"], "Retry")
                            wait = 30
            except Exception:
                log.exception("Metadata processing failed")
                wait = 30
            self.wake.wait(wait)
            self.wake.clear()
