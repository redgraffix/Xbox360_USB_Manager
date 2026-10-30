# Xbox 360 USB Manager

A local Windows app for converting and transferring Xbox 360 game files to an external USB drive. It watches an input folder, processes one item at a time, verifies the output, and keeps a searchable USB game list and import history.

The window title is **Xbox 360 Game Converter**. Download the source using **Code → Download ZIP**, extract it, and follow the steps below. You can run the Python app directly or build a Windows executable. There is no prebuilt installer in this repository.

## What it does

- Imports Xbox 360 disc images, ZIP/7Z archives, extracted game folders, and supported Xbox content packages.
- Shows each file's stage, progress, errors, and completion status.
- Checks existing files before reporting duplicates; conflicting content is retained for review.
- Lets you choose the input folder, USB volume, game subfolder, and temporary storage location.
- Offers **Keep source files**, **Delete source files**, or **Ask before deleting**. New installations default to **Keep**.
- Lists USB games, opens their folders, verifies recorded imports, and removes explicitly selected components.
- Keeps import history separately from the visible queue. Clearing completed/error rows does not delete files or history.

The app works with local files and makes no network requests. It does not download games, modify console firmware, format drives, install trainers, or bypass content licensing. Use files you are authorized to use. The console must already support the content you are transferring.

## Requirements

- Windows 10 or 11; Windows is the supported platform.
- Python 3.11 or later with Tkinter (included in the normal [Python Windows installer](https://www.python.org/downloads/windows/)).
- A current [7-Zip Windows installation](https://www.7-zip.org/download.html). Keep it updated: it parses the archives you supply.
- An external drive already formatted as FAT32, with sufficient free space. The app does not format it. A single output file cannot exceed FAT32's 4 GiB limit.
- Temporary storage on NTFS or exFAT when extracting ISO files larger than 4 GiB from an archive.

There are no third-party Python runtime packages to install, no account requirement, and no administrator requirement for running the app. 7-Zip and Python binaries are not bundled in this repository.

## Run from source

Install Python and 7-Zip, download the repository using **Code → Download ZIP**, and **Extract All**. Open the extracted folder containing `app.py`, right-click inside it, choose **Open in Terminal**, and run:

```powershell
py -3 app.py
```

Alternatively, with Git installed:

```powershell
git clone https://github.com/redgraffix/Xbox360_USB_Manager.git
cd Xbox360_USB_Manager
py -3 app.py
```

7-Zip is located in the standard Program Files installation directories. If it is installed elsewhere, supply a trusted executable explicitly:

```powershell
py -3 app.py --seven-zip 'E:\Utilities\7-Zip\7z.exe'
```

The `XBOX360_7ZIP` environment variable is an alternative for the app and tests. The app deliberately does not search the current directory or `PATH` for executables.

The default app data/input folder is `XBOX360-Convert` inside your Windows home folder. To keep a separate profile:

```powershell
py -3 app.py --base "$env:LOCALAPPDATA\Xbox360USBManager"
```

`--base` controls where settings and history live. Changing the input folder in Settings does not relocate that history. Use a dedicated folder, separate from this source checkout, and do not share it publicly.

## Build a Windows executable (optional)

Run these commands in the extracted repository on Windows. Python 3.12 is used by the automated checks; a standard Python installation with Tkinter is recommended.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

Open `dist\Xbox360 Converter\Xbox360 Converter.exe`. Keep the **entire** `Xbox360 Converter` folder, including `_internal`; the executable alone will not work. Python is included in this build, so it is not required on the receiving PC. That PC still needs 7-Zip installed separately. The build is unsigned and does not install anything or format a drive.

The build uses [PyInstaller's one-folder bundle](https://pyinstaller.org/en/stable/usage.html) and includes only application code and its runtime, not your input files, settings, history, USB identity, or logs. It copies the README and security notes into the output. Build dependencies and intermediate files stay in the ignored `.venv` and `build` directories; you can remove those after building. Keep `dist` if you want to keep the executable.

To update an existing copy, close the app and rebuild or replace its complete application folder. Settings and history stay in your per-user `XBOX360-Convert` folder (or your explicit `--base` location). Both source and executable launches use the same default profile. A fresh profile starts paused, has no USB selected, and keeps originals.

## First import

1. Connect the FAT32 USB drive and start the app. The queue starts paused.
2. Open **Settings**. Choose the **Input folder**, **USB drive**, **Games subfolder** (normally `Games`), and **Temporary files** folder.
3. Choose what happens to source files after verification. Start with **Keep source files** until you have tested your setup.
4. Place finished archives, ISOs, or prepared game folders in the input folder. There is no drag-and-drop target inside the app.
5. Click **Start queue**. New files are added automatically when that option is enabled. Temporary browser downloads are ignored; inputs must stop changing before they queue.
6. After completion, close the app and safely eject the USB through Windows. Add its `Games` and `Content` locations to your dashboard's scan paths as appropriate.

Copy large folders into a staging location outside the input folder, then move them into the input folder after the copy completes. The settling timer is a convenience, not a guarantee that a paused copy has finished.

**Pause** finishes the current import and prevents the next one from starting. **Stop** cancels the current operation and retains its source. On restart the queue is paused; use **Retry selected** for an interrupted job.

## Supported inputs and output layout

| Input | Handling |
| --- | --- |
| Xbox 360 `.iso` | Extracts the supported Xbox filesystem into a game folder containing `default.xex`. |
| `.zip` / `.7z` | Inspects the archive, extracts it, and processes supported content. An archive can contain multiple discs or content packages. |
| Extracted game folder | Copies the files; no ISO conversion is needed. |
| Games on Demand (GOD) | Imports a descriptor and its matching `.data` directory. Put both in one input folder. |
| Supported STFS packages | Routes recognized arcade, DLC/install content, and title-update packages using their metadata. |
| Unknown, encrypted, linked, incomplete, or conflicting content | Retains the original and reports an error or **Needs review**. |

Typical output, with placeholder names/IDs:

```text
USB root/
  Games/
    Example Game/default.xex
    Example Multi-disc Game/Disc 1/default.xex
  Content/0000000000000000/<title-id>/<content-type>/...
  Xbox360-Conversion-Staging/Converter/<job-id>/...
```

Installation/add-on discs are kept separate from the playable disc. Some games still require their installation step on the Xbox. Title updates must match the game and be activated on the console; legacy `TU_` packages require manual placement. Standalone system updates are left for review.

Verification checks that output files match the input. It does **not** prove that a game, update, or DLC package will run, is licensed, or is compatible with a particular softmod. Launch the game on the console to test it.

## Storage, cleanup, and duplicate handling

Imports are sequential. ISO files are extracted once onto USB, compared byte-for-byte against the ISO, and published by renaming the verified staging folder. They need the extracted content size plus a reserve; prepared archives may still need roughly twice their extracted size. Archived ISOs additionally need room in the selected temporary folder. Progress describes the current stage, not an overall time guarantee.

File inventory checks report progress and respond to Stop. An interrupted ISO stage can be reused only after full verification against the ISO; incomplete stages are rebuilt. Stop retains unfinished temporary work for retry or manual cleanup. Normal completion cleans temporary work when that setting is enabled. Disk flushes and full byte verification remain enabled.

For an explicitly requested unattended restart, `--retry-interrupted --start-queue` retries interrupted inputs and starts the queue. Normal launches still start paused.

- **Keep source files:** retains the input after a successful import.
- **Delete source files:** permanently deletes the input only after content verification and successful recording of the result.
- **Ask before deleting:** retains the input until confirmation; later cleanup rechecks recorded output hashes.
- **Clean temporary files automatically:** removes this app's per-job work folders after processing. Settings also offers cleanup of recognized leftovers.
- Failed, ambiguous, and wholly duplicate inputs are retained. Existing differing files are not overwritten.
- Standalone informational sidecars such as README text and artwork may be ignored; files inside an extracted game folder remain part of the game. Keep any sidecars you want before enabling source deletion.

Deletion does not use the Recycle Bin. Keep another copy of anything you cannot replace. Removing a USB game also requires a component selection and confirmation; DLC and updates are separate selections. Saved profiles and saves are not listed as game components.

An older import without saved checksums cannot be fully verified from history; reimport its original instead. A changed USB drive letter can affect old recorded absolute paths even though the selected volume is recognized by identity.

## Local data and privacy

The app makes no network requests and has no telemetry or account integration. Local data includes personal filesystem paths, volume labels/identifiers, game names, and operation history:

| Location under the app base | Contents |
| --- | --- |
| `_state/settings.json` | Folder choices, USB identity, cleanup preferences |
| `_state/queue.json` | Queue and history |
| `_state/manifests/` | Verification records and file hashes |
| `_state/activity.log` | Activity log; rotates at approximately 1 MiB with two older copies |
| `_work/` | Default temporary location; configurable |

These files, game content, binaries, credentials, and build outputs are excluded by `.gitignore`. **Do not attach raw logs, settings, history, screenshots, or verification manifests to a public issue.** Redact personal paths, identifiers, and sensitive filenames first. A `.gitignore` rule does not remove files already committed, and can be bypassed with `git add -f`.

See [SECURITY.md](SECURITY.md) for the trust model, limitations, and reporting guidance.

## Development and checks

The source uses the Python standard library. Install 7-Zip before running the tests. From the repository root:

```powershell
py -3 -m unittest -v test_converter test_imports test_security test_performance
py -3 test_ui.py
py -3 test_v2_ui.py
py -3 test_ui_regression.py
py -3 scripts/check_public_tree.py
```

Tests use small synthetic disc/package fixtures and temporary folders with a fake USB drive. They do not download games, require real games, or write to a physical USB. The three UI scripts briefly open test windows and require an interactive Windows desktop. The automated workflow runs the non-GUI tests, repository hygiene check, and an executable build/import smoke test on Windows.

| File | Responsibility |
| --- | --- |
| `app.py` | Tkinter interface and settings |
| `engine.py` | Queue, history, import orchestration, cleanup |
| `core.py` | Xbox filesystem parsing, path checks, byte verification, Windows locking |
| `content.py` | Archive inspection, content metadata, prepared-file copies |
| `library.py` | USB inventory and selected-component removal |
| `runtime.py` | Per-user defaults and explicit/trusted-location 7-Zip lookup |
| `scripts/build_windows.py` | Optional Windows executable build |

Read [CONTRIBUTING.md](CONTRIBUTING.md) before adding fixtures or publishing diagnostics.

## License and affiliation

A project license has not been selected yet. Public repository visibility alone does not grant an open-source license. Python, Tcl/Tk, PyInstaller, and 7-Zip remain under their own licenses. No runtime binaries or game files are stored in this repository. Review the applicable runtime licenses before redistributing a build.

This is an independent utility, not affiliated with or endorsed by Microsoft, Xbox, or game publishers.
