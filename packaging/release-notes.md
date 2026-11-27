Aether is a local game library for Windows 11. Add a directory of offline-installed games, review the executable for each installation, and launch it from the library. Search, artwork, collections, favorites and play history stay available offline. An optional personal RAWG key adds metadata and artwork.

This update fixes Close and Cancel in desktop dialogs, preserves a “Keep local” decision during a background RAWG lookup, and prevents counting a gap between game sessions after restarting Aether. Launch profiles now show when the approved executable is missing.

- **Installer** — download the file ending in `-setup.exe` for a per-user installation.
- **Portable** — extract the entire `-portable.zip` archive and run `Aether.exe`.
- **Checksums** — `SHA256SUMS.txt` lists both release files.

The installer and portable app use the same per-user library. The native window uses Microsoft Edge WebView2. These binaries are unsigned.
