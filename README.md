# Aether

A local-first NiceGUI desktop library for PC games. Aether discovers independent installations and games stored in storefront folders through the filesystem. Search, launch, play history, personal edits and cached artwork work offline.

## Install on Windows 11

[Download the latest release](https://github.com/tfassih/aether/releases/latest). Run the x64 setup executable for a per-user installation, or extract the entire portable ZIP and open **Aether.exe**. Both use the same library in `%LOCALAPPDATA%\Aether`. Windows 11 requires the Microsoft Edge WebView2 runtime for the native window. If your Windows image omits it, [install the Evergreen runtime](https://developer.microsoft.com/microsoft-edge/webview2/). The downloads include Python and application dependencies. Release binaries are unsigned.

## Add and launch a game

1. Choose **Add folder**. For a directory such as `D:\Games` or `SteamLibrary\steamapps\common`, select **Library folder**. If you choose the game's own directory, select **Single game installation**.
2. Open a discovered game. Choose **Add profile**, review the executable and working directory, and select **Approve profile**. You can enter a relative executable manually, choose from ranked candidates, or browse in the desktop app. Launch arguments are entered one per line.
3. Select **Play**. Keep Aether open to record play sessions. Settings also lets you track approved executables started outside Aether. Storefront-dependent games retain their original client and DRM requirements.
4. In **Settings**, optionally save your own [RAWG API key](https://rawg.io/apidocs). Unambiguous PC titles match automatically. Open **Discovery** to choose a match or keep a local record. Aether caches metadata, backgrounds and up to six screenshots. **Add artwork** accepts a personal cover, background, logo or screenshot.
5. Star a game, tag it or assign it to collections in **Edit game**. Use **Filters** for genre, platform, tag, sorting and cover size. Switch grid/list view beside the search box. Right-click a game for common actions.

## Folders and data

Watchdog coalesces changes to the affected installation. Aether also reconciles at startup, after a drive reconnects, on demand, and periodically while open. Each source has a search depth, exclusions and pause control. Missing installations and removed sources keep their customized entries and play history. A recognized directory move on the same volume preserves its installation and relative profiles. Matching two copies to the same RAWG game groups them in one entry. A copy moved across volumes can have a new filesystem identity.

Data and logs are held in `%LOCALAPPDATA%\Aether`; portable refers to the application distribution. To back up, close Aether and copy the data directory. Set `AETHER_DATA_DIR` before launch to use an alternate location. Windows stores a saved RAWG key in Credential Manager. `RAWG_API_KEY` is also supported. Online lookups are optional. Monitoring and Play use only local paths, and the desktop server binds to loopback.

## Run from source

Use Python 3.12 (x64 on Windows).

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python main.py
```

For browser development, run `python main.py --browser`. On Linux use browser mode. Use `--no-open --port 8080` for UI automation. Desktop launching targets Windows executables.

## Test and build

On Windows x64, install [Inno Setup 6.3 or later](https://jrsoftware.org/isinfo.php), activate the virtual environment and run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python -m pytest -q
python -m ruff check .
.\packaging\build.ps1
```

The installer, portable ZIP and SHA-256 manifest are written to `dist/release`. The GitHub Actions workflow builds the x64 binaries and exercises both distributions on a Windows x64 runner and a separate Windows 11 Arm runner using x64 emulation. Both machines check the native WebView2 interface, silent install/uninstall and data persistence. When the version in `VERSION` has no published release, a successful workflow on `main` publishes that commit and the verified files. Change `VERSION` and the release notes together for a new release.

Aether uses the license in [LICENSE](LICENSE). External game metadata and artwork are provided by [RAWG](https://rawg.io/) and their respective owners.
