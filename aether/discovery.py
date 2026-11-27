"""Read-only game discovery. Observations never imply permission to launch."""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import pefile

SKIP_DIRS = {
    "_commonredist",
    "_redist",
    "redist",
    "redistributables",
    "directx",
    "support",
    "uninstall",
    "__installer",
    "easyanticheat",
    "battleye",
    "node_modules",
    ".git",
    "windows",
    "$recycle.bin",
    "system volume information",
    "dotnet",
}
UTILITY = re.compile(
    r"^(?:unins(?:tall)?\d*|setup(?:[-_ .].*)?|install(?:er)?|dxsetup|vcredist.*|"
    r"vc_redist.*|crash(?:pad|report|handler).*|unitycrashhandler.*|ue[45]prereq.*|"
    r"easyanticheat.*|dotnet.*|ffmpeg|7z[ag]?|pythonw?|vc_runtime.*)$",
    re.IGNORECASE,
)


def norm(value: str) -> str:
    return "".join(c for c in value.casefold() if c.isalnum())


def clean_title(value: str) -> str:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value.replace("_", " "))
    value = re.sub(r"\s*[\[(](?:gog|steam|x(?:64|86)|v[\d.]+)[\])]\s*", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split()) or "Untitled"


def path_key(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def file_identity(path: Path) -> str:
    info = path.stat()
    return f"{info.st_dev}:{info.st_ino}" if info.st_ino else ""


def checked_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    rel = Path(relative)
    if not relative or rel.is_absolute() or rel.drive or ".." in rel.parts:
        raise ValueError("Choose a path inside the installation.")
    path = root
    # Check ancestors too: a monitored directory may have been replaced with a junction.
    for part in [*reversed(root.parents), root]:
        if linked(part):
            raise ValueError("The installation passes through a link or junction.")
    for part in rel.parts:
        path /= part
        if linked(path):
            raise ValueError("Choose a file inside the installation, outside links and junctions.")
    if directory and path.is_dir():
        return path
    if not directory and path.is_file() and path.suffix.casefold() == ".exe":
        return path
    raise ValueError("The selected folder or game executable is unavailable.")


def pe_info(path: Path) -> tuple[str, str]:
    if path.stat().st_size > 192 * 1024 * 1024:
        return "", ""
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                return "", ""
        with pefile.PE(str(path), fast_load=True) as pe:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
            for block in getattr(pe, "FileInfo", []):
                for item in block:
                    for table in getattr(item, "StringTable", []):

                        def field(name: bytes, entries=table.entries) -> str:
                            return entries.get(name, b"").decode("utf-8", "replace").strip("\x00 ")[:160]

                        return field(b"ProductName"), field(b"ProductVersion")
    except (pefile.PEFormatError, OSError, ValueError, AttributeError, IndexError):
        pass
    return "", ""


@dataclass(frozen=True)
class Observation:
    path: Path
    identity: str
    title: str
    candidates: list[str]
    version: str


def executable_rank(relative: str, game: str) -> tuple[int, str]:
    p = Path(relative)
    name, title = norm(p.stem), norm(game)
    score = 100 if name == title else 45 if title and (title in name or name in title) else 0
    score += 20 if len(p.parts) == 1 else 0
    score += 16 if "shipping" in name else 0
    score += 6 if "win64" in relative.casefold() else 0
    score -= 12 if "launcher" in name or "editor" in name else 0
    return -score, relative.casefold()


def observe(path: Path, depth: int, ignored: set[str]) -> Observation:
    candidates = []

    def fail(error: OSError) -> None:
        raise error

    for base, dirs, files in os.walk(path, followlinks=False, onerror=fail):
        here = Path(base)
        level = len(here.relative_to(path).parts)
        dirs[:] = [
            name
            for name in dirs
            if level < depth
            and name.casefold() not in SKIP_DIRS
            and path_key(here / name) not in ignored
            and not linked(here / name)
        ]
        for name in files:
            exe = here / name
            if exe.suffix.casefold() != ".exe" or UTILITY.fullmatch(exe.stem):
                continue
            if path_key(exe) not in ignored and not linked(exe) and exe.is_file():
                candidates.append(exe.relative_to(path).as_posix())
    candidates.sort(key=lambda p: executable_rank(p, path.name))
    product, version = pe_info(path / candidates[0]) if candidates else ("", "")
    title = clean_title(path.name)
    if product and norm(product) not in {
        "unrealengine",
        "unrealengine4",
        "unrealengine5",
        "unity",
        "bootstrappackagedgame",
    }:
        title = product
    return Observation(path, file_identity(path), title, candidates, version)
