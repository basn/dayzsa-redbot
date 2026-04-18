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

## Notes
- This is a lightweight ops/history log for quick context.
- Keep entries append-only and include commit IDs for traceability.
