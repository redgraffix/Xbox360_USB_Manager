# Xbox 360 USB Manager

A Windows app for preparing Xbox 360 game files and transferring them to a **USB drive, a local folder, or an Xbox over FTP**. Choose an input folder and a destination, then start the queue. The app processes one item at a time and verifies the destination before optionally deleting the original.

The window is called **Xbox 360 Game Manager**. This repository provides source and a build script, not a prebuilt installer.

## Features

- Watches for completed ISO, ZIP/7Z, extracted-game and supported content-package inputs.
- Unpacks and converts in local working storage. No conversion folder or special partition is created on the USB or Xbox.
- Imports prepared games, GOD, arcade, DLC/install content and supported title updates into their appropriate locations.
- Pauses the queue when a destination disconnects; retains originals and local preparation for retry.
- Shows a cached Games list with small available thumbnails, name, size, content type, disc count and **Location**: Local folder, PC USB, Xbox HDD or Xbox USB.
- Keeps history when completed/error rows are cleared. Includes manual refresh, verification and confirmed component removal.
- Uses a flat dark interface with green accents, readable hover states and stable tab sizes.

The app does not download games, format drives, modify console firmware or install trainers. Supply files you are authorized to use; the console must already support the content.

## Requirements and quick start

- Windows 10/11. Linux is not currently supported.
- Python 3.11+ with Tkinter, or a locally built executable.
- A current [7-Zip Windows installation](https://www.7-zip.org/download.html).
- Enough local working space for the unpacked input and prepared game. Keep a reserve for Windows.
- USB output requires a preformatted FAT32 volume. Individual files must fit its 4 GiB limit.
- FTP output requires the Xbox's FTP service, address, login and a reachable storage folder.

Download **Code → Download ZIP**, extract it, open Terminal in the folder containing `app.py`, and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Runtime packages are **ttkbootstrap** for themed controls and **Pillow** for thumbnails. There is no account, administrator requirement, telemetry or game-download service.

1. Open **Settings**. Choose an existing **Input folder** and a local **Working folder**.
2. Choose **Local folder**, **USB drive** or **Xbox FTP** as the destination. Select it explicitly; a disconnected destination never silently falls back to another location.
3. For FTP, enter the Xbox address, port, username and password. Use **Test connection** and **Browse Xbox** to select `Hdd1`, `Usb0`, or a folder within that storage. Passwords are saved in Windows Credential Manager.
4. Keep **Games subfolder** as `Games` unless you have a reason to change it. GOD, DLC and updates use `Content` alongside that folder.
5. Choose **Keep source files**, **Delete source files**, or **Ask before deleting**. Fresh installations keep originals.
6. Put completed files/folders in the input folder and click **Start queue**. There is no in-window drag-and-drop target.
7. After importing, scan the corresponding Games/Content paths in Aurora. Close the manager before safely ejecting a USB.

Browser partial downloads are ignored. The settling timer cannot prove that a paused copy has finished: copy large folders elsewhere first, then move the completed folder into the watched input.

**Pause** finishes the current import. **Stop** interrupts it and keeps its original and prepared work. After a destination error the queue waits, retries with a delay up to 60 seconds, and prevents later games from starting. **Retry now** requests an immediate attempt. Normal app restarts always start paused; retry interrupted entries when ready.

## Storage and verification

The default working folder is `%LOCALAPPDATA%\Xbox360USBManager\Work`; it is configurable. Preparation and verification happen there before transfer. Large archive imports can temporarily need both unpacked content and a prepared copy. Archived disc images are unpacked and prepared sequentially.

Destination layout:

```text
Selected destination/
  Games/
    Example Game/default.xex
    Example Multi-disc Game/Disc 1/default.xex
  Content/0000000000000000/<title-id>/<content-type>/...
```

Each transferred file is written as a job-owned `.x360-….part`, read back and checked with SHA-256, then renamed into place. These short-lived transfer files are not conversion scratch space. Executables and GOD descriptors are published after their payloads. A multi-file game is not an atomic transaction; don't scan or launch it until the job reports Complete.

Interrupted work reuses verified local preparation. Completed destination files are checked before reuse; an incomplete file is retransferred from its beginning. Destination identity and Games subfolder remain bound to that job. Return to its original destination before retrying it. The app does not silently overwrite differing files.

FTP verification downloads the uploaded bytes again to calculate their hash. This is deliberate and can be slow over Wi-Fi. Original deletion waits for verification and durable records. A successful transfer proves byte consistency, not game compatibility or playability; test it on the console.

- **Keep source files:** retain the original.
- **Delete source files:** permanently delete only after successful verification and recording.
- **Ask before deleting:** prompt later and reverify before deletion.
- Successful imports always clean their local working files. Interrupted work remains available for retry. **Settings → Clean temporary files** removes recognized app-owned leftovers, including work retained for interrupted jobs.
- Failed, ambiguous and wholly duplicate inputs are retained. Informational sidecars outside game folders may be ignored; preserve them separately if wanted.
- Deletion does not use the Recycle Bin. Component removal requires explicit selection and confirmation, and rechecks the destination inventory. DLC and updates are separate selections.

Keep another copy of anything you cannot replace. Disk failure, power loss and concurrent writes from other applications remain outside the guarantees of file verification.

## Formats and game list

| Input | Handling |
| --- | --- |
| Xbox 360 ISO | Extracts its supported Xbox filesystem into a playable folder layout. |
| ZIP / 7Z | Inspects and unpacks supported discs or prepared content. |
| Extracted game folder | Copies and verifies without ISO conversion. |
| GOD | Imports descriptor and matching `.data` folder; keep them together in one input folder. |
| Recognized STFS packages | Routes arcade, DLC/install content and title updates by their metadata. |
| Unknown, incomplete, linked or conflicting input | Retains the source for review. |

Install/add-on discs may still require installation on the console. Title updates must match the game and be activated in Aurora; legacy `TU_` files need manual placement. Standalone system updates are left for review.

Games lists are cached separately for each selected destination. Opening the tab reads the cache. **Refresh list** performs a quick background inventory: it lists the Games folder and recognized Content directories, without descending into game assets, reading package payloads, or hashing files. Game rows appear as soon as the top-level listing is available. Names and other details reuse local import history and the cache; unknown sizes show **—** instead of triggering a deep scan. Cached sizes and metadata can be stale.

Imports update the cache, which remains viewable while the destination is disconnected. Full byte checks happen during transfers and explicit verification. Removal first inspects only the selected content, then asks for confirmation with its current file count and size, and rechecks before deleting. Duplicate detection treats cached data as hints and checks actual destination bytes before declaring a duplicate.

During imports, thumbnails are cached from supported package headers or existing `folder.jpg`, `folder.png`, `cover.jpg`, `cover.png` or `icon.png` files. Quick refresh reuses those thumbnails; a neutral Xbox-style placeholder appears when cached artwork is unavailable. Nothing is scraped or downloaded. Metadata is best-effort: a folder name may be used when a readable title is unavailable. The list shows the selected destination, not an aggregated view of every device.

## App data and privacy

The profile defaults to `XBOX360-Convert` in your Windows home folder. Settings may point the watched input elsewhere without moving history. Use a separate profile with:

```powershell
.\.venv\Scripts\python.exe app.py --base "$env:LOCALAPPDATA\Xbox360USBManager"
```

7-Zip is located in standard Program Files folders. Use `--seven-zip` with a trusted absolute executable path, or `XBOX360_7ZIP`, for a custom installation. The app does not search the current folder or PATH for executables.

| Private location | Contents |
| --- | --- |
| `<profile>/_state/settings.json` | Input/work/output choices, device identity, FTP host and username; no password |
| `<profile>/_state/queue.json` | Jobs, transfer journal and history |
| `<profile>/_state/manifests/` | Verification hashes and destination records |
| `<profile>/_state/libraries/` | Per-destination game-list caches |
| `<profile>/_state/thumbnails/` | Small cached images |
| `<profile>/_state/activity.log` | Rotating activity log, about 1 MiB plus two older files |
| Windows Credential Manager | FTP password, scoped to host, port and username |
| Selected working folder | Per-job preparation, removed on completion |

FTP is plain, unencrypted FTP for a trusted local network. Do not expose it to the internet. The app connects only to the configured destination; it has no telemetry or online artwork service.

Private settings, logs, history, images, game files and build outputs do not belong in the public repository. Redact paths, addresses and identifiers before sharing diagnostics. See [SECURITY.md](SECURITY.md).

## Build an executable

On Windows, from the repository root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

Run `dist\Xbox360 Converter\Xbox360 Converter.exe`. Keep the **entire** application folder, including `_internal`. The receiving PC needs 7-Zip; Python is included in the bundle. Builds are unsigned. No user settings, history, games or credentials are bundled. Close the app before replacing its application folder; keep the separate profile intact.

Build intermediates stay in ignored `build` and `.venv` folders; these can be removed after building. Keep `dist` if you need the built app. For an explicitly unattended restart, `--retry-interrupted --start-queue` retries interrupted inputs and starts work; normal startup stays paused.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest -v test_converter test_imports test_security test_performance test_destinations
.\.venv\Scripts\python.exe test_ui.py
.\.venv\Scripts\python.exe test_v2_ui.py
.\.venv\Scripts\python.exe test_ui_regression.py
.\.venv\Scripts\python.exe scripts\check_public_tree.py
```

Tests use synthetic fixtures, temporary folders and a loopback FTP server. They never need real games or a real Xbox. UI tests open isolated windows. Windows CI runs non-GUI tests, public-tree checks and an executable build smoke test.

| Module | Responsibility |
| --- | --- |
| `app.py`, `settings_ui.py`, `theme.py` | Interface, settings and styling |
| `engine.py`, `pipeline.py` | Persistent queue and import lifecycle |
| `preparation.py` | Local unpacking and game preparation |
| `destinations.py`, `transfers.py` | Filesystem/FTP adapters and verified transfer journal |
| `credentials.py` | Windows Credential Manager integration |
| `library_cache.py` | Cached inventory, thumbnails and guarded removal |
| `core.py`, `content.py` | Filesystem parsing, archive/content detection and verification |
| `runtime.py` | Defaults and trusted 7-Zip discovery |

Read [CONTRIBUTING.md](CONTRIBUTING.md) before publishing changes. Windows-only credential and device code is isolated from the transfer/preparation logic, but cross-platform support is not implemented.

## License and affiliation

A project license has not been selected. Public visibility alone does not grant an open-source license. Python, Tcl/Tk, ttkbootstrap, Pillow, PyInstaller and 7-Zip retain their own licenses; review them before redistributing builds. This independent utility is not affiliated with Microsoft, Xbox or game publishers.
