# AGENTS

This file tracks what has been changed in this repo during agent-assisted work.

## 2026-04-17

### Full-alert repost flap fix
- Commit: `c0b051f`
- File: `dayz_monitor/dayz_monitor.py`
- Change summary:
  - Added a 10-minute non-full hold timer before resetting `last_full`.
  - Prevents reposting when population briefly flaps `100 -> 99 -> 100`.
  - Added `not_full_since` state tracking per server.

### Alert channel removal support
- Commit: `cc96140`
- File: `dayz_monitor/dayz_monitor.py`
- Change summary:
  - `dayz channel` now supports clearing alert channel with keywords:
    - `remove`, `clear`, `off`, `none`, `disable`, `disabled`
  - Channel argument now accepts mention/ID/name via converter.
  - `dayz list` shows `alerts: disabled` when no alert channel is set.

## 2026-04-18

### Scheduled restart up-notify with 1s checks
- Commit: `2a552e4`
- File: `dayz_monitor/dayz_monitor.py`
- Change summary:
  - Added a dedicated 1-second restart watcher task.
  - Added `dayz restart <name> <hours>` to configure restart hours (`0-23`) per server.
  - Sends "back online" notifications only when at least one non-bot user is in voice.
  - Reuses each server's existing alert channel for restart notifications.
- Added restart schedule visibility in `dayz list`.

## 2026-06-20

### Local Python/test workflow notes
- Added a local test workflow recommendation for this environment:
  - Python is available via Nix, not from `python`/`python3` in PATH.
  - Run tests with: `nix run nixpkgs#python313 -- -m pytest`.
  - Example targeted run: `nix run nixpkgs#python313 -- -m pytest -q tests/test_dayz_monitor.py`.

## 2026-08-02

### Aftermath public statistics and team roster
- Commit: `8ce3295`
- Files: `dayz_monitor/dayz_monitor.py`, `tests/test_dayz_monitor.py`, `README.md`
- Change summary:
  - Added per-monitored-server Aftermath API UUID configuration with `dayz aftermath`.
  - Added public `dayz group` and `dayz player` statistics lookups.
  - Added a per-guild team roster of up to six SteamID64s and `dayz teamstats` aggregate reporting.
- Validation: `PYTHONPATH=. nix run nixpkgs#python313Packages.pytest -- -q tests/test_dayz_monitor.py` returned `16 passed, 2 skipped`.

## Notes
- This is a lightweight ops/history log for quick context.
- Keep entries append-only and include commit IDs for traceability.
