# Build from the repository root: python -m PyInstaller --clean --noconfirm packaging/aether.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
assets = [(str(root / "aether" / "assets"), "aether/assets"),
          (str(root / "VERSION"), "."), (str(root / "LICENSE"), ".")]
assets += collect_data_files("nicegui", include_py_files=True)
assets += collect_data_files("webview") + copy_metadata("nicegui")
analysis = Analysis(
    [str(root / "main.py")], pathex=[str(root)], datas=assets, binaries=[],
    hiddenimports=["engineio.async_drivers.asgi", "webview.platforms.edgechromium", "webview.platforms.winforms",
                   "keyring.backends.Windows", "watchdog.observers.read_directory_changes", "socksio"],
    excludes=["pytest", "ruff", "tkinter", "PyQt5", "PyQt6", "PySide6", "gi"],
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="Aether",
          icon=str(root / "aether/assets/aether.ico"), console=False, upx=False)
dist = COLLECT(exe, analysis.binaries, analysis.datas, name="Aether", upx=False)
