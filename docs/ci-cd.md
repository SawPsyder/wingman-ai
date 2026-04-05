# CI/CD Pipeline

Wingman AI uses a two-stage build pipeline: **Core** is built first (Python via PyInstaller), then bundled as a sidecar into the **Client** (Tauri desktop app).

## Architecture

```
Core repo (this repo)                    Client repo (private)
────────────────────                     ────────────────────
1. Push to ci-cd branch                 3. Client workflows pull Core from staging
   or trigger manually                  4. Tauri bundles Core as sidecar
2. PyInstaller builds per platform      5. Platform-specific installers created
   → uploaded to R2 staging             6. Uploaded to R2 releases + latest.json
                                        7. Tauri auto-updater picks up new versions
```

## Core Build Workflows

Each platform has its own reusable workflow, called by `dev-build.yml`:

| Workflow | Runner | Output |
|----------|--------|--------|
| `release-windows.yml` | `windows-latest` | Signed `.exe` + `_internal/` |
| `release-macos.yml` | `macos-latest` | `WingmanAiCore` binary + `_internal/` |
| `release-linux.yml` | `ubuntu-latest` | `WingmanAiCore` binary + `_internal/` |

### What each build does

1. **Checkout** the repo
2. **Install system deps** (macOS: `portaudio sdl2`, Linux: `portaudio19-dev libsdl2-dev`)
3. **Set up Python 3.11.7** and install `requirements.txt` in a venv
4. **PyInstaller** builds `WingmanAiCore.spec` → produces `dist/WingmanAiCore/`
5. **Windows only**: code-signed via DigiCert SSM (two-job pipeline: build → sign)
6. **Upload** to R2 staging under `core/{git-sha}/{platform}/`
7. **Update** `latest-{platform}.txt` pointer so Client can resolve `latest`

### Triggering builds

- **Manual**: Go to Actions → "Dev Build" → select platform (windows/macos/linux/all)
- **Tag push**: Windows builds trigger on any tag push (`release-windows.yml`)

## Version Management

Core version lives in `services/system_manager.py`:

```python
LOCAL_VERSION = "3.0.1"
```

This version is:
- Extracted during CI for artifact naming
- Reported to the Client at runtime via the API
- Independent of the Client version (but should stay in sync for user-facing releases)

When bumping the version, update `LOCAL_VERSION` in `system_manager.py`. The Client also has its own version in `package.json` and `tauri.conf.json` — these must match for the Tauri auto-updater to work correctly.

## Staging Artifacts

Core builds are uploaded to an R2 staging bucket, keyed by git SHA:

```
core/{sha}/{platform}/
  WingmanAiCore(.exe)     ← main binary
  _internal/              ← PyInstaller dependencies (Python runtime, packages, etc.)
```

A `latest-{platform}.txt` file points to the most recent SHA, so the Client can fetch the latest build without knowing the specific commit.

## Platform Notes

### Windows
- Code-signed with DigiCert SSM via a two-job pipeline (build on `windows-latest`, sign on `windows-2022`)
- The unsigned intermediate artifact is deleted after signing

### macOS
- No code signing yet (planned: Apple Developer Account)
- Users must run `xattr -cr /Applications/WingmanAI.app` until signing is set up
- System deps: `portaudio` (PyAudio) and `sdl2` (pygame)
- `pyobjc-framework-Quartz` is a pip dependency (Python bindings to native Quartz), not a brew package

### Linux
- System deps: `portaudio19-dev` (PyAudio) and `libsdl2-dev` (pygame)
- No code signing

## How the Client Consumes Core

The Client repo has a `download-core` action that:
1. Resolves `latest` (or a specific SHA) from the staging bucket
2. Downloads the full `dist/WingmanAiCore/` directory
3. Moves the main binary into `src-tauri/binaries/` with a Tauri-compatible target triple name
4. Downloads shared model files (faster-whisper, pocket-tts) from a separate R2 path

The Client then builds around these artifacts using Tauri, creating platform-specific installers (`.exe` setup for Windows, `.dmg` for macOS, `.AppImage` for Linux).
