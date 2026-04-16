import asyncio
import contextlib
from typing import Any, Dict, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box


class DayZMonitor(commands.Cog):
    """Monitor DayZ SA Launcher population and alert when servers become full."""

    API_BASE = "https://dayzsalauncher.com/api/v2/launcher/players"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9013470171, force_registration=True)
        self.config.register_guild(servers={}, check_interval=60)
        self.session: Optional[aiohttp.ClientSession] = None
        self._task = self.bot.loop.create_task(self._monitor_loop())

    def cog_unload(self):
        if self._task:
            self._task.cancel()
        self.bot.loop.create_task(self._cleanup())

    async def _cleanup(self):
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.session and not self.session.closed:
            await self.session.close()

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
                raise
            except Exception:
                await asyncio.sleep(30)

    async def _check_guild(self, guild: discord.Guild):
        data = await self.config.guild(guild).servers()
        if not data:
            return

        changed = False
        for name, server in data.items():
            address = server.get("address")
            channel_id = server.get("channel_id")
            if not address or not channel_id:
                continue

            try:
                payload = await self._fetch_server_data(address)
                parsed = self._parse_population(payload)
            except Exception:
                continue

            is_full = parsed["is_full"]
            if is_full is None:
                continue

            last_full = bool(server.get("last_full", False))
            if is_full and not last_full:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.send(
                        f":rotating_light: **{name}** is now full.\n"
                        f"Online: `{parsed['online']}/{parsed['max_players']}` | "
                        f"Queue: `{parsed['queue']}`"
                    )
            server["last_full"] = is_full
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
        await ctx.send(f"Removed `{removed.get('name', key)}`.")

    @dayz_group.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_channel(self, ctx: commands.Context, name: str, channel: discord.TextChannel):
        """Set the alert channel for a monitored server."""
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        if key not in servers:
            await ctx.send(f"No monitored server named `{key}`.")
            return
        servers[key]["channel_id"] = channel.id
        await self.config.guild(ctx.guild).servers.set(servers)
        await ctx.send(f"Alert channel for `{servers[key].get('name', key)}` set to {channel.mention}.")

    @dayz_group.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_interval(self, ctx: commands.Context, seconds: int):
        """Set monitor check interval in seconds (minimum 30)."""
        seconds = max(30, seconds)
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"Check interval set to `{seconds}` seconds.")

    @dayz_group.command(name="list")
    async def dayz_list(self, ctx: commands.Context):
        """List monitored servers."""
        servers = await self.config.guild(ctx.guild).servers()
        if not servers:
            await ctx.send("No servers configured yet.")
            return

        lines = []
        for key, server in servers.items():
            channel = ctx.guild.get_channel(server.get("channel_id", 0))
            channel_text = channel.mention if channel else f"(missing channel `{server.get('channel_id')}`)"
            lines.append(f"- `{server.get('name', key)}` -> `{server.get('address')}` | alerts: {channel_text}")
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
