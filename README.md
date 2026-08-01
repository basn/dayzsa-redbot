# dayzcog

Minimal Redbot cog repo for monitoring DayZ SA Launcher server population and queue.

## Included Cog

- `dayz_monitor`

## Install In Red

1. Add this repo:
   - `[p]repo add dayzcogs https://github.com/<you>/<repo>`
2. Install cog:
   - `[p]cog install dayzcogs dayz_monitor`
3. Load cog:
   - `[p]load dayz_monitor`

## Basic Setup

- Add a server:
  - `[p]dayz add main 91.134.31.223:27017 #alerts`
  - Use the server query port. For many DayZ servers this is not the game port.
- Check one server:
  - `[p]dayz status main`
- Check all:
  - `[p]dayz statusall`
- List configured servers:
  - `[p]dayz list`

## Admin Commands

- Remove server:
  - `[p]dayz remove <name>`
- Set alert channel:
  - `[p]dayz channel <name> <#channel>`
- Set check interval (seconds, min 30):
  - `[p]dayz interval <seconds>`
- Publish one configured server's population and queue in the bot's Discord status:
  - `[p]dayz presence <name>`
  - Disable with `[p]dayz presence off`

## Queue Status

The DayZ SA Launcher player endpoint only returns online and max player counts for
some servers. This cog also queries the server directly with Steam A2S_INFO and
reads DayZ's `lqsN` keyword when present, where `N` is the live queue size.

## Local test notes

- Unit tests are offline and do not require any live servers:
  - `PYTHONPATH=. nix run nixpkgs#python313Packages.pytest -- -q tests/test_dayz_monitor.py`
- Run live integration checks against one real server by setting:
  - `DAYZ_MONITOR_LIVE_SERVER=<host:query_port>`
  - `PYTHONPATH=. nix run nixpkgs#python313Packages.pytest -- -q tests/test_dayz_monitor.py -k live`
