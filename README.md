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
