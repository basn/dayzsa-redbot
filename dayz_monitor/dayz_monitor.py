import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.dayz_monitor")


class DayZMonitor(commands.Cog):
    """Monitor DayZ SA Launcher population and alert when servers become full."""

    API_BASE = "https://dayzsalauncher.com/api/v2/launcher/players"
    NON_FULL_RESET_SECONDS = 10 * 60
    RESTART_WATCH_MAX_SECONDS = 30 * 60

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9013470171, force_registration=True)
        self.config.register_guild(servers={}, check_interval=60)
        self.session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None
        self._restart_runtime: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._start_monitor()

    def _start_monitor(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._monitor_loop())
            log.info("DayZ monitor task started.")
        if self._restart_task is None or self._restart_task.done():
            self._restart_task = asyncio.create_task(self._restart_watch_loop())
            log.info("DayZ restart watcher task started.")

    def cog_unload(self):
        if self._task:
            self._task.cancel()
        if self._restart_task:
            self._restart_task.cancel()
        self.bot.loop.create_task(self._cleanup())

    async def _cleanup(self):
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._restart_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_task
        if self.session and not self.session.closed:
            await self.session.close()
            log.info("DayZ monitor HTTP session closed.")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _fetch_server_data(self, address: str) -> Dict[str, Any]:
        session = await self._get_session()
        url = f"{self.API_BASE}/{address}"
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json(content_type=None)
            if not isinstance(data, dict):
                raise RuntimeError("Unexpected API response format.")
            return data

    @staticmethod
    def _pick_int(data: Dict[str, Any], candidates: Tuple[str, ...]) -> Optional[int]:
        for key in candidates:
            if key in data:
                try:
                    return int(data[key])
                except (TypeError, ValueError):
                    continue
        return None

    def _parse_population(self, payload: Dict[str, Any]) -> Dict[str, Optional[int]]:
        # API field names vary; this supports common variants, including:
        # {"status":0,"result":{"players":57,"maxPlayers":100}}
        source = payload
        result = payload.get("result")
        if isinstance(result, dict):
            source = result

        online = self._pick_int(
            source,
            ("players", "numplayers", "online", "playerCount", "Players", "NumPlayers"),
        )
        max_players = self._pick_int(
            source,
            ("maxplayers", "maxPlayers", "slots", "MaxPlayers", "max"),
        )
        queue = self._pick_int(
            source,
            ("queue", "queuePlayers", "Queue", "waiting", "waitingPlayers"),
        )

        free_slots = None
        is_full = None
        if online is not None and max_players is not None:
            free_slots = max(max_players - online, 0)
            is_full = online >= max_players

        return {
            "online": online,
            "max_players": max_players,
            "queue": queue if queue is not None else 0,
            "free_slots": free_slots,
            "is_full": is_full,
        }

    def _format_status(self, name: str, address: str, parsed: Dict[str, Optional[int]]) -> str:
        online = parsed["online"]
        max_players = parsed["max_players"]
        queue = parsed["queue"]
        free_slots = parsed["free_slots"]

        if online is None or max_players is None:
            return (
                f"**{name}** (`{address}`)\n"
                f"Could not parse player/max values from API response.\n"
                f"Queue: `{queue}`"
            )

        return (
            f"**{name}** (`{address}`)\n"
            f"Online: `{online}/{max_players}`\n"
            f"Free slots: `{free_slots}`\n"
            f"Queue: `{queue}`"
        )

    @staticmethod
    def _has_human_in_voice(guild: discord.Guild) -> bool:
        channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        for channel in channels:
            for member in channel.members:
                if not member.bot:
                    return True
        return False

    @staticmethod
    def _parse_restart_hours_input(raw: str) -> Optional[list]:
        parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
        if not parts:
            return None

        hours = []
        for p in parts:
            if not p.isdigit():
                return None
            h = int(p)
            if h < 0 or h > 23:
                return None
            hours.append(h)

        return sorted(set(hours))

    @staticmethod
    def _format_restart_hours(hours: list) -> str:
        if not hours:
            return "disabled"
        return ", ".join(f"{h:02d}:00" for h in hours)

    @staticmethod
    def _normalize_restart_hours(raw: Any) -> list:
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            try:
                hour = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                out.append(hour)
        return sorted(set(out))

    async def _monitor_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                for guild in self.bot.guilds:
                    await self._check_guild(guild)
                    await asyncio.sleep(1)
                # Use shortest configured interval across guilds for responsiveness.
                intervals = []
                for guild in self.bot.guilds:
                    i = await self.config.guild(guild).check_interval()
                    intervals.append(max(30, int(i)))
                await asyncio.sleep(min(intervals) if intervals else 60)
            except asyncio.CancelledError:
                log.info("DayZ monitor task cancelled.")
                raise
            except Exception:
                log.exception("Unhandled exception in DayZ monitor loop; retrying in 30s.")
                await asyncio.sleep(30)

    async def _restart_watch_loop(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                for guild in self.bot.guilds:
                    await self._check_guild_restart_watch(guild)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                log.info("DayZ restart watcher task cancelled.")
                raise
            except Exception:
                log.exception("Unhandled exception in DayZ restart watcher loop; retrying in 5s.")
                await asyncio.sleep(5)

    async def _check_guild_restart_watch(self, guild: discord.Guild):
        data = await self.config.guild(guild).servers()
        if not data:
            return

        now_dt = datetime.now()
        now_ts = int(time.time())
        slot_key = now_dt.strftime("%Y%m%d%H")
        has_voice = self._has_human_in_voice(guild)

        for name, server in data.items():
            address = server.get("address")
            channel_id = server.get("channel_id")
            restart_hours = self._normalize_restart_hours(server.get("restart_hours"))
            if not address or not channel_id or not restart_hours:
                self._restart_runtime.pop((guild.id, name), None)
                continue

            runtime = self._restart_runtime.setdefault(
                (guild.id, name),
                {"waiting": False, "slot_key": None, "saw_down": False, "down_since": None, "started_at": None},
            )

            if now_dt.minute == 0 and now_dt.hour in restart_hours and runtime.get("slot_key") != slot_key:
                runtime["slot_key"] = slot_key
                if has_voice:
                    runtime["waiting"] = True
                    runtime["saw_down"] = False
                    runtime["down_since"] = None
                    runtime["started_at"] = now_ts

            if not runtime.get("waiting"):
                continue

            started_at = runtime.get("started_at") or now_ts
            if now_ts - int(started_at) > self.RESTART_WATCH_MAX_SECONDS:
                runtime["waiting"] = False
                runtime["saw_down"] = False
                runtime["down_since"] = None
                continue

            if not has_voice:
                continue

            is_up = False
            parsed: Dict[str, Optional[int]] = {"online": None, "max_players": None, "queue": 0}
            try:
                payload = await self._fetch_server_data(address)
                parsed = self._parse_population(payload)
                is_up = parsed["online"] is not None and parsed["max_players"] is not None
            except Exception:
                is_up = False

            if not is_up:
                if not runtime.get("saw_down"):
                    runtime["saw_down"] = True
                    runtime["down_since"] = now_ts
                continue

            if not runtime.get("saw_down"):
                # Avoid false positives when restart is delayed and server never dropped.
                continue

            channel = guild.get_channel(channel_id)
            if channel:
                down_since = runtime.get("down_since") or now_ts
                downtime = max(now_ts - int(down_since), 0)
                await channel.send(
                    f":white_check_mark: **{name}** appears back online after restart.\n"
                    f"Online: `{parsed['online']}/{parsed['max_players']}` | "
                    f"Queue: `{parsed['queue']}` | "
                    f"Downtime: `{downtime}s`"
                )

            runtime["waiting"] = False
            runtime["saw_down"] = False
            runtime["down_since"] = None

    async def _check_guild(self, guild: discord.Guild):
        data = await self.config.guild(guild).servers()
        if not data:
            return

        changed = False
        now = int(time.time())
        for name, server in data.items():
            address = server.get("address")
            channel_id = server.get("channel_id")
            if not address or not channel_id:
                continue

            try:
                payload = await self._fetch_server_data(address)
                parsed = self._parse_population(payload)
            except Exception:
                log.exception(
                    "Failed to query DayZ server '%s' (%s) for guild %s (%s).",
                    name,
                    address,
                    guild.name,
                    guild.id,
                )
                continue

            is_full = parsed["is_full"]
            if is_full is None:
                continue

            last_full = bool(server.get("last_full", False))
            not_full_since = server.get("not_full_since")

            if is_full:
                if not last_full:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        await channel.send(
                            f":rotating_light: **{name}** is now full.\n"
                            f"Online: `{parsed['online']}/{parsed['max_players']}` | "
                            f"Queue: `{parsed['queue']}`"
                        )
                    server["last_full"] = True
                    changed = True
                if not_full_since is not None:
                    server["not_full_since"] = None
                    changed = True
            else:
                if last_full:
                    if not_full_since is None:
                        server["not_full_since"] = now
                        changed = True
                    else:
                        try:
                            non_full_duration = now - int(not_full_since)
                        except (TypeError, ValueError):
                            non_full_duration = 0
                            server["not_full_since"] = now
                            changed = True

                        if non_full_duration >= self.NON_FULL_RESET_SECONDS:
                            server["last_full"] = False
                            server["not_full_since"] = None
                            changed = True
                elif not_full_since is not None:
                    server["not_full_since"] = None
                    changed = True

        if changed:
            await self.config.guild(guild).servers.set(data)

    @commands.group(name="dayz")
    @commands.guild_only()
    async def dayz_group(self, ctx: commands.Context):
        """DayZ SA Launcher monitoring commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @dayz_group.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_add(
        self,
        ctx: commands.Context,
        name: str,
        address: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Add a server to monitor.

        Example: [p]dayz add main 91.134.31.223:27017 #alerts
        """
        channel = channel or ctx.channel
        key = name.lower()

        servers = await self.config.guild(ctx.guild).servers()
        if key in servers:
            await ctx.send(f"A server named `{key}` already exists.")
            return

        try:
            payload = await self._fetch_server_data(address)
            parsed = self._parse_population(payload)
        except Exception as exc:
            await ctx.send(f"Could not fetch API data for `{address}`: `{exc}`")
            return

        servers[key] = {
            "name": name,
            "address": address,
            "channel_id": channel.id,
            "last_full": bool(parsed.get("is_full", False)),
            "not_full_since": None,
            "restart_hours": [],
        }
        await self.config.guild(ctx.guild).servers.set(servers)
        await ctx.send(
            f"Added `{name}` (`{address}`) and set alert channel to {channel.mention}.\n"
            + self._format_status(name, address, parsed)
        )

    @dayz_group.command(name="remove", aliases=["del", "delete"])
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_remove(self, ctx: commands.Context, name: str):
        """Remove a monitored server by name."""
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        if key not in servers:
            await ctx.send(f"No monitored server named `{key}`.")
            return
        removed = servers.pop(key)
        await self.config.guild(ctx.guild).servers.set(servers)
        self._restart_runtime.pop((ctx.guild.id, key), None)
        await ctx.send(f"Removed `{removed.get('name', key)}`.")

    @dayz_group.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_channel(
        self,
        ctx: commands.Context,
        name: str,
        channel: Optional[str] = None,
    ):
        """Set or clear the alert channel for a monitored server.

        Examples:
        - [p]dayz channel main #alerts
        - [p]dayz channel main remove
        """
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        if key not in servers:
            await ctx.send(f"No monitored server named `{key}`.")
            return

        if channel is None or channel.lower() in {"remove", "clear", "off", "none", "disable", "disabled"}:
            servers[key]["channel_id"] = None
            await self.config.guild(ctx.guild).servers.set(servers)
            await ctx.send(
                f"Alert channel for `{servers[key].get('name', key)}` cleared. Full alerts are now disabled."
            )
            return

        try:
            resolved_channel = await commands.TextChannelConverter().convert(ctx, channel)
        except commands.BadArgument:
            await ctx.send(
                "Invalid channel. Mention a text channel (or provide channel ID/name), or use `remove` to clear."
            )
            return

        servers[key]["channel_id"] = resolved_channel.id
        await self.config.guild(ctx.guild).servers.set(servers)
        await ctx.send(f"Alert channel for `{servers[key].get('name', key)}` set to {resolved_channel.mention}.")

    @dayz_group.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_interval(self, ctx: commands.Context, seconds: int):
        """Set monitor check interval in seconds (minimum 30)."""
        seconds = max(30, seconds)
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"Check interval set to `{seconds}` seconds.")

    @dayz_group.command(name="restart")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_restart(self, ctx: commands.Context, name: str, *, hours: str):
        """Set or clear hourly restart watch times for a monitored server.

        Times use the bot host's local timezone and should be hour values 0-23.
        Example: [p]dayz restart main 1,4,7,10,13,16,19,22
        Clear:   [p]dayz restart main off
        """
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        if key not in servers:
            await ctx.send(f"No monitored server named `{key}`.")
            return

        if hours.lower() in {"remove", "clear", "off", "none", "disable", "disabled"}:
            servers[key]["restart_hours"] = []
            await self.config.guild(ctx.guild).servers.set(servers)
            self._restart_runtime.pop((ctx.guild.id, key), None)
            await ctx.send(f"Restart watch for `{servers[key].get('name', key)}` disabled.")
            return

        parsed_hours = self._parse_restart_hours_input(hours)
        if parsed_hours is None:
            await ctx.send("Invalid hours. Use comma/space separated values between `0` and `23` (e.g. `1,4,7,10`).")
            return

        servers[key]["restart_hours"] = parsed_hours
        await self.config.guild(ctx.guild).servers.set(servers)
        self._restart_runtime.pop((ctx.guild.id, key), None)
        await ctx.send(
            f"Restart watch for `{servers[key].get('name', key)}` set to: `{self._format_restart_hours(parsed_hours)}`.\n"
            "When at least one non-bot user is in voice, the cog checks every second after those hours until the server returns."
        )

    @dayz_group.command(name="list")
    async def dayz_list(self, ctx: commands.Context):
        """List monitored servers."""
        servers = await self.config.guild(ctx.guild).servers()
        if not servers:
            await ctx.send("No servers configured yet.")
            return

        lines = []
        for key, server in servers.items():
            channel_id = server.get("channel_id")
            if not channel_id:
                channel_text = "disabled"
            else:
                channel = ctx.guild.get_channel(channel_id)
                channel_text = channel.mention if channel else f"(missing channel `{channel_id}`)"
            restart_hours = self._normalize_restart_hours(server.get("restart_hours"))
            restart_text = self._format_restart_hours(restart_hours)
            lines.append(
                f"- `{server.get('name', key)}` -> `{server.get('address')}` | "
                f"alerts: {channel_text} | restarts: {restart_text}"
            )
        await ctx.send(box("\n".join(lines), lang="md"))

    @dayz_group.command(name="status", aliases=["query", "online"])
    async def dayz_status(self, ctx: commands.Context, name: str):
        """Show online/free slots/queue for one configured server."""
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        server = servers.get(key)
        if not server:
            await ctx.send(f"No monitored server named `{key}`.")
            return

        address = server["address"]
        try:
            payload = await self._fetch_server_data(address)
            parsed = self._parse_population(payload)
        except Exception as exc:
            await ctx.send(f"Could not fetch status for `{address}`: `{exc}`")
            return

        await ctx.send(self._format_status(server.get("name", key), address, parsed))

    @dayz_group.command(name="statusall", aliases=["all"])
    async def dayz_status_all(self, ctx: commands.Context):
        """Show online/free slots/queue for all configured servers."""
        servers = await self.config.guild(ctx.guild).servers()
        if not servers:
            await ctx.send("No servers configured yet.")
            return

        blocks = []
        for key, server in servers.items():
            address = server.get("address")
            if not address:
                continue
            try:
                payload = await self._fetch_server_data(address)
                parsed = self._parse_population(payload)
                blocks.append(self._format_status(server.get("name", key), address, parsed))
            except Exception as exc:
                blocks.append(f"**{server.get('name', key)}** (`{address}`)\nError: `{exc}`")

        await ctx.send("\n\n".join(blocks[:10]))
