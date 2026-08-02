import asyncio
import contextlib
import logging
import re
import socket
import struct
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.dayz_monitor")


class DayZMonitor(commands.Cog):
    """Monitor DayZ SA Launcher population and alert when servers become full."""

    API_BASE = "https://dayzsalauncher.com/api/v2/launcher/players"
    AFTERMATH_API_BASE = "https://aftermath-gaming.com/api/v1"
    A2S_INFO_QUERY = b"\xff\xff\xff\xffTSource Engine Query\x00"
    A2S_RULES_QUERY = b"\xff\xff\xff\xffV"
    NON_FULL_RESET_SECONDS = 10 * 60
    RESTART_WATCH_MAX_SECONDS = 30 * 60
    TEAM_MAX_MEMBERS = 6

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9013470171, force_registration=True)
        self.config.register_guild(servers={}, check_interval=60, team_members=[])
        # Discord presence is bot-wide, so only one configured server can
        # drive it at a time.
        self.config.register_global(presence_server=None)
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

    async def _fetch_aftermath_data(self, path: str) -> Dict[str, Any]:
        session = await self._get_session()
        url = f"{self.AFTERMATH_API_BASE}/{path.lstrip('/')}"
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json(content_type=None)
            if not isinstance(data, dict):
                raise RuntimeError("Unexpected Aftermath API response format.")
            return data

    async def _fetch_aftermath_group_stats(self, server_id: str, group_name: str) -> Dict[str, Any]:
        return await self._fetch_aftermath_data(
            f"group/getStats/{quote(group_name, safe='')}/{quote(server_id, safe='')}"
        )

    async def _fetch_aftermath_player_stats(self, server_id: str, steam_id: str) -> Dict[str, Any]:
        return await self._fetch_aftermath_data(
            f"player/getStats/{quote(steam_id, safe='')}/{quote(server_id, safe='')}"
        )

    @staticmethod
    def _format_aftermath_stats(subject: str, data: Dict[str, Any]) -> str:
        def value(key: str, fallback: str = "?") -> str:
            raw = data.get(key, fallback)
            return str(raw) if raw is not None else fallback

        lines = [
            f"**{subject}**",
            f"Kills: `{value('total_kills', value('TotalKills'))}` | "
            f"Deaths: `{value('total_deaths', value('TotalDeaths'))}` | "
            f"K/D: `{value('kd_ratio', value('KDRatio'))}`",
            f"Longest kill: `{value('longest_kill', value('LongestKill'))} m`",
        ]
        return "\n".join(lines)

    @staticmethod
    def _normalize_team_members(raw_members: Any) -> list:
        if not isinstance(raw_members, list):
            return []
        members = []
        seen = set()
        for raw in raw_members:
            if not isinstance(raw, dict):
                continue
            steam_id = str(raw.get("steam_id", ""))
            if not re.fullmatch(r"\d{17}", steam_id) or steam_id in seen:
                continue
            seen.add(steam_id)
            name = str(raw.get("name") or steam_id)
            members.append({"steam_id": steam_id, "name": name[:80]})
        return members

    @staticmethod
    def _format_team_stats(results: list) -> str:
        total_kills = 0
        total_deaths = 0
        longest_kill = 0.0
        lines = ["**Team statistics**"]
        for member, data in results:
            kills = int(data.get("total_kills", 0) or 0)
            deaths = int(data.get("total_deaths", 0) or 0)
            longest = float(data.get("longest_kill", 0) or 0)
            total_kills += kills
            total_deaths += deaths
            longest_kill = max(longest_kill, longest)
            kd_ratio = "∞" if deaths == 0 and kills else f"{kills / deaths:.2f}" if deaths else "0.00"
            lines.append(f"- **{member['name']}**: `{kills}` K / `{deaths}` D | `{kd_ratio}` K/D")

        team_kd = "∞" if total_deaths == 0 and total_kills else f"{total_kills / total_deaths:.2f}" if total_deaths else "0.00"
        lines.insert(
            1,
            f"Total: `{total_kills}` K / `{total_deaths}` D | `{team_kd}` K/D | "
            f"Longest kill: `{longest_kill:.2f} m`",
        )
        return "\n".join(lines)

    @staticmethod
    def _parse_address(address: str) -> Tuple[str, int]:
        host, sep, raw_port = address.rpartition(":")
        if not sep or not host or not raw_port:
            raise ValueError("Expected address in `host:query_port` format.")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("Expected numeric query port in address.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Query port must be between 1 and 65535.")
        return host.strip("[]"), port

    async def _a2s_request(self, host: str, port: int, payload: bytes, timeout: float = 5.0, retries: int = 2) -> bytes:
        last_error = None
        for _ in range(max(1, retries)):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Redbot uses uvloop, which raises NotImplementedError for
                # loop.sock_sendto() on this UDP socket.  Run the short,
                # blocking datagram exchange in a worker so it works with both
                # asyncio's default loop and uvloop without blocking Discord.
                sock.settimeout(timeout)
                await asyncio.to_thread(sock.sendto, payload, (host, port))
                data, _ = await asyncio.to_thread(sock.recvfrom, 65535)
                return data
            except (OSError, TimeoutError) as exc:
                last_error = exc
            finally:
                sock.close()
        raise last_error

    @staticmethod
    def _read_cstring(data: bytes, pos: int) -> Tuple[str, int]:
        end = data.find(b"\x00", pos)
        if end == -1:
            raise ValueError("Unterminated A2S string.")
        return data[pos:end].decode("utf-8", "replace"), end + 1

    @staticmethod
    def _queue_from_keywords(keywords: str) -> Optional[int]:
        for keyword in keywords.replace(";", ",").split(","):
            trimmed = keyword.strip()
            match = re.search(
                r"(?:lqs|queue|queueplayers|waiting(?:players)?)[\s:=\-\x00,;]*(\d+)",
                trimmed,
                flags=re.IGNORECASE,
            )
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _queue_from_bytes(data: bytes) -> Optional[int]:
        match = re.search(
            rb"(?:lqs|queue|queueplayers|waiting(?:players)?)[\s:=\-\x00,;]*(\d+)",
            data,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1).decode("ascii", "ignore"))

        match = re.search(
            rb"queue(?:players)?[\s:=\-\x00,;]*(\d+)",
            data,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1).decode("ascii", "ignore"))
        return None

    def _queue_from_rules(self, data: bytes) -> Optional[int]:
        if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff" or data[4] != 0x45:
            return self._queue_from_bytes(data)

        pos = 5
        try:
            num_rules = struct.unpack_from("<H", data, pos)[0]
        except struct.error:
            return self._queue_from_bytes(data)
        pos += 2

        for _ in range(num_rules):
            try:
                key, pos = self._read_cstring(data, pos)
                value, pos = self._read_cstring(data, pos)
            except ValueError:
                return self._queue_from_bytes(data)

            if key:
                queue = self._queue_from_keywords(key)
                if queue is not None:
                    return queue

            if value:
                queue = self._queue_from_keywords(value)
                if queue is not None:
                    return queue

            normalized_key = key.lower().strip()
            if normalized_key in {"queue", "waiting", "waitingplayers", "queueplayers", "lqs"}:
                try:
                    return int(value)
                except ValueError:
                    continue

        return self._queue_from_bytes(data)

    def _parse_a2s_info(self, data: bytes) -> Dict[str, Optional[int]]:
        if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff" or data[4] != 0x49:
            raise ValueError("Unexpected A2S_INFO response.")

        pos = 5
        pos += 1  # protocol
        _, pos = self._read_cstring(data, pos)  # name
        _, pos = self._read_cstring(data, pos)  # map
        _, pos = self._read_cstring(data, pos)  # folder
        _, pos = self._read_cstring(data, pos)  # game
        if pos + 7 > len(data):
            raise ValueError("Truncated A2S_INFO response.")

        pos += 2  # app id
        online = data[pos]
        pos += 1
        max_players = data[pos]
        pos += 1
        pos += 5  # bots, server type, environment, visibility, VAC
        _, pos = self._read_cstring(data, pos)  # version

        queue = None
        if pos < len(data):
            edf = data[pos]
            pos += 1
            if edf & 0x80:
                pos += 2
            if edf & 0x10:
                pos += 8
            if edf & 0x40:
                pos += 2
                _, pos = self._read_cstring(data, pos)
            if edf & 0x20:
                keywords, pos = self._read_cstring(data, pos)
                queue = self._queue_from_keywords(keywords)
            if edf & 0x01:
                pos += 8

        if queue is None:
            queue = self._queue_from_bytes(data)

        return {"online": online, "max_players": max_players, "queue": queue}

    async def _fetch_a2s_info(self, address: str, *, trace: bool = False) -> Dict[str, Optional[int]]:
        host, port = self._parse_address(address)
        try:
            data = await self._a2s_request(host, port, self.A2S_INFO_QUERY)
            if len(data) >= 9 and data[:4] == b"\xff\xff\xff\xff" and data[4] == 0x41:
                data = await self._a2s_request(host, port, self.A2S_INFO_QUERY + data[5:9])
        except Exception:
            if trace:
                log.info("A2S_INFO request failed for %s; trying A2S_RULES fallback.", address, exc_info=True)
            try:
                rules = await self._a2s_request(host, port, self.A2S_RULES_QUERY)
            except Exception:
                if trace:
                    log.info("A2S_RULES fallback request also failed for %s.", address, exc_info=True)
                return {"online": None, "max_players": None, "queue": None}

            if len(rules) >= 9 and rules[:4] == b"\xff\xff\xff\xff" and rules[4] == 0x41:
                try:
                    rules = await self._a2s_request(host, port, self.A2S_RULES_QUERY + rules[5:9])
                except Exception:
                    if trace:
                        log.info("A2S_RULES challenge retry failed for %s.", address, exc_info=True)
                    return {"online": None, "max_players": None, "queue": None}

            queue = self._queue_from_rules(rules)
            if trace and queue is None:
                log.info("A2S_RULES payload for %s did not include queue-related fields.", address)
            return {"online": None, "max_players": None, "queue": queue}

        parsed = {"online": None, "max_players": None, "queue": self._queue_from_bytes(data)}
        raw_queue = parsed["queue"]
        try:
            parsed_info = self._parse_a2s_info(data)
            for key in ("online", "max_players"):
                if parsed_info.get(key) is not None:
                    parsed[key] = parsed_info[key]
            if parsed_info.get("queue") is not None:
                parsed["queue"] = parsed_info["queue"]
            else:
                parsed["queue"] = raw_queue
        except Exception:
            if trace:
                log.info(
                    "Failed to fully parse A2S_INFO payload for %s; keeping raw queue fallback (%s).",
                    address,
                    raw_queue,
                    exc_info=True,
                )
            log.debug("Failed to fully parse A2S_INFO for %s:%s; using raw queue fallback.", host, port, exc_info=True)

        if parsed.get("queue") is None:
            if trace:
                log.info("A2S_INFO payload had no queue; trying A2S_RULES fallback for %s.", address)
            try:
                rules = await self._a2s_request(host, port, self.A2S_RULES_QUERY)
            except Exception:
                if trace:
                    log.info("A2S_RULES fallback after parse for %s failed.", address, exc_info=True)
                return parsed

            if len(rules) >= 9 and rules[:4] == b"\xff\xff\xff\xff" and rules[4] == 0x41:
                try:
                    rules = await self._a2s_request(host, port, self.A2S_RULES_QUERY + rules[5:9])
                except Exception:
                    if trace:
                        log.info("A2S_RULES challenge retry after parse for %s failed.", address, exc_info=True)
                    return parsed

            if parsed.get("queue") is None:
                queue = self._queue_from_rules(rules)
                if trace and queue is None:
                    log.info("A2S_RULES fallback for %s did not expose queue fields.", address)
                parsed["queue"] = queue

        return parsed

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
            ("queue", "lqs", "queuePlayers", "Queue", "waiting", "waitingPlayers"),
        )

        free_slots = None
        is_full = None
        if online is not None and max_players is not None:
            free_slots = max(max_players - online, 0)
            is_full = online >= max_players

        return {
            "online": online,
            "max_players": max_players,
            "queue": queue,
            "free_slots": free_slots,
            "is_full": is_full,
        }

    async def _fetch_status(self, address: str, *, trace: bool = False) -> Dict[str, Optional[int]]:
        api_error = None
        api_queue = None
        try:
            payload = await self._fetch_server_data(address)
            parsed = self._parse_population(payload)
            api_queue = parsed["queue"]
            if parsed["online"] is not None and parsed["max_players"] is not None and parsed["queue"] is not None:
                return parsed
        except Exception as exc:
            api_error = exc
            parsed = {
                "online": None,
                "max_players": None,
                "queue": None,
                "free_slots": None,
                "is_full": None,
            }

        try:
            a2s = await self._fetch_a2s_info(address, trace=trace)
        except Exception:
            if trace:
                if api_error is not None:
                    log.info(
                        "A2S_INFO+RULES diagnostics skipped for %s because API fetch had already failed.",
                        address,
                        exc_info=True,
                    )
                else:
                    log.info("Failed to query A2S_INFO for status command on %s.", address, exc_info=True)
            if api_error is not None:
                raise api_error
            return parsed

        for key in ("online", "max_players", "queue"):
            if a2s.get(key) is not None:
                parsed[key] = a2s[key]

        online = parsed["online"]
        max_players = parsed["max_players"]
        if online is not None and max_players is not None:
            parsed["free_slots"] = max(max_players - online, 0)
            parsed["is_full"] = online >= max_players

        if trace and parsed["queue"] is None:
            log.info(
                "Queue still unresolved after API/A2S status resolution for %s (api_queue=%s, a2s_queue=%s, online=%s, max=%s).",
                address,
                api_queue,
                a2s.get("queue"),
                parsed["online"],
                parsed["max_players"],
            )

        return parsed

    @staticmethod
    def _format_queue_if_present(queue: Optional[int]) -> Optional[str]:
        if queue is None:
            return None
        return f"Queue: `{queue}`"

    def _format_status(self, name: str, address: str, parsed: Dict[str, Optional[int]]) -> str:
        online = parsed["online"]
        max_players = parsed["max_players"]
        queue = parsed["queue"]
        free_slots = parsed["free_slots"]

        if online is None or max_players is None:
            lines = [
                f"**{name}** (`{address}`)",
                "Could not parse player/max values from status response.",
            ]
        else:
            lines = [
                f"**{name}** (`{address}`)",
                f"Online: `{online}/{max_players}`",
                f"Free slots: `{free_slots}`",
            ]

        queue_line = self._format_queue_if_present(queue)
        if queue_line is not None:
            lines.append(queue_line)
        return "\n".join(lines)

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
                await self._update_presence()
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

    @staticmethod
    def _format_presence(name: str, parsed: Dict[str, Optional[int]]) -> str:
        online = parsed.get("online")
        max_players = parsed.get("max_players")
        queue = parsed.get("queue")
        if online is None or max_players is None:
            return f"{name}: offline"

        text = f"{name}: {online}/{max_players}"
        if queue is not None:
            text += f" | Queue: {queue}"
        return text

    async def _update_presence(self):
        configured = await self.config.presence_server()
        if not isinstance(configured, dict):
            return

        guild_id = configured.get("guild_id")
        key = configured.get("server")
        if not isinstance(guild_id, int) or not isinstance(key, str):
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        servers = await self.config.guild(guild).servers()
        server = servers.get(key)
        if not isinstance(server, dict) or not server.get("address"):
            return

        try:
            parsed = await self._fetch_status(server["address"])
            text = self._format_presence(server.get("name", key), parsed)
        except Exception:
            log.exception("Failed to update Discord presence from DayZ server '%s'.", key)
            text = f"{server.get('name', key)}: offline"

        await self.bot.change_presence(activity=discord.Game(name=text))

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
            parsed: Dict[str, Optional[int]] = {"online": None, "max_players": None, "queue": None}
            try:
                parsed = await self._fetch_status(address)
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
                    f"{self._format_queue_if_present(parsed['queue']) + ' | ' if parsed['queue'] is not None else ''}"
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
                parsed = await self._fetch_status(address)
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
                        queue_suffix = (
                            f" | {self._format_queue_if_present(parsed['queue'])}"
                            if parsed["queue"] is not None
                            else ""
                        )
                        await channel.send(
                            f":rotating_light: **{name}** is now full.\n"
                            f"Online: `{parsed['online']}/{parsed['max_players']}`"
                            f"{queue_suffix}"
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
            parsed = await self._fetch_status(address)
        except Exception as exc:
            await ctx.send(f"Could not fetch status for `{address}`: `{exc}`")
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

    @dayz_group.command(name="presence", aliases=["botstatus"])
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_presence(self, ctx: commands.Context, name: str):
        """Publish one server's population and queue in the bot's Discord status.

        Example: [p]dayz presence main
        Disable: [p]dayz presence off
        """
        if name.lower() in {"remove", "clear", "off", "none", "disable", "disabled"}:
            await self.config.presence_server.set(None)
            await self.bot.change_presence(activity=None)
            await ctx.send("DayZ Discord status publishing disabled.")
            return

        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        if key not in servers:
            await ctx.send(f"No monitored server named `{key}`.")
            return

        await self.config.presence_server.set({"guild_id": ctx.guild.id, "server": key})
        await self._update_presence()
        await ctx.send(
            f"Bot status will now show population and queue for `{servers[key].get('name', key)}`."
        )

    @dayz_group.command(name="aftermath")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_aftermath(self, ctx: commands.Context, name: str, server_id: str):
        """Set the Aftermath API server ID for a monitored server.

        Example: [p]dayz aftermath main 1ed9f69d-4ee3-6dac-b18b-ea5e938a80e2
        """
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        if key not in servers:
            await ctx.send(f"No monitored server named `{key}`.")
            return
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", server_id):
            await ctx.send("Invalid Aftermath server ID. It must be a UUID.")
            return

        servers[key]["aftermath_server_id"] = server_id.lower()
        await self.config.guild(ctx.guild).servers.set(servers)
        await ctx.send(f"Aftermath stats for `{servers[key].get('name', key)}` are now configured.")

    async def _get_aftermath_server_id(self, ctx: commands.Context, name: str) -> Optional[str]:
        key = name.lower()
        servers = await self.config.guild(ctx.guild).servers()
        server = servers.get(key)
        if not server:
            await ctx.send(f"No monitored server named `{key}`.")
            return None
        server_id = server.get("aftermath_server_id")
        if not server_id:
            await ctx.send(
                f"No Aftermath API server ID is configured for `{server.get('name', key)}`. "
                f"An admin can set it with `{ctx.clean_prefix}dayz aftermath {key} <server-id>`.")
            return None
        return str(server_id)

    @dayz_group.group(name="team")
    async def dayz_team(self, ctx: commands.Context):
        """Manage this Discord server's Aftermath team roster."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @dayz_team.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_team_add(self, ctx: commands.Context, steam_id: str, *, name: Optional[str] = None):
        """Add a player to the team roster (maximum six)."""
        if not re.fullmatch(r"\d{17}", steam_id):
            await ctx.send("Steam ID must be a 17-digit SteamID64.")
            return
        team = self._normalize_team_members(await self.config.guild(ctx.guild).team_members())
        if any(member["steam_id"] == steam_id for member in team):
            await ctx.send(f"`{steam_id}` is already on the team roster.")
            return
        if len(team) >= self.TEAM_MAX_MEMBERS:
            await ctx.send(f"The team roster is limited to `{self.TEAM_MAX_MEMBERS}` players.")
            return
        team.append({"steam_id": steam_id, "name": (name or steam_id).strip()[:80] or steam_id})
        await self.config.guild(ctx.guild).team_members.set(team)
        await ctx.send(f"Added `{team[-1]['name']}` to the team roster (`{len(team)}/{self.TEAM_MAX_MEMBERS}`).")

    @dayz_team.command(name="remove", aliases=["del", "delete"])
    @commands.admin_or_permissions(manage_guild=True)
    async def dayz_team_remove(self, ctx: commands.Context, steam_id: str):
        """Remove a Steam ID from the team roster."""
        team = self._normalize_team_members(await self.config.guild(ctx.guild).team_members())
        updated = [member for member in team if member["steam_id"] != steam_id]
        if len(updated) == len(team):
            await ctx.send(f"`{steam_id}` is not on the team roster.")
            return
        await self.config.guild(ctx.guild).team_members.set(updated)
        await ctx.send(f"Removed `{steam_id}` from the team roster.")

    @dayz_team.command(name="list")
    async def dayz_team_list(self, ctx: commands.Context):
        """List the team roster."""
        team = self._normalize_team_members(await self.config.guild(ctx.guild).team_members())
        if not team:
            await ctx.send("The team roster is empty.")
            return
        lines = [f"- `{member['name']}` — `{member['steam_id']}`" for member in team]
        await ctx.send(box("\n".join(lines), lang="md"))

    @dayz_group.command(name="teamstats")
    async def dayz_team_stats(self, ctx: commands.Context, server: str):
        """Show aggregate and individual public Aftermath statistics for the team."""
        team = self._normalize_team_members(await self.config.guild(ctx.guild).team_members())
        if not team:
            await ctx.send(
                f"The team roster is empty. An admin can add players with "
                f"`{ctx.clean_prefix}dayz team add <steam-id> [name]`."
            )
            return
        server_id = await self._get_aftermath_server_id(ctx, server)
        if server_id is None:
            return
        fetched = await asyncio.gather(
            *(self._fetch_aftermath_player_stats(server_id, member["steam_id"]) for member in team),
            return_exceptions=True,
        )
        results = []
        failures = []
        for member, response in zip(team, fetched):
            if isinstance(response, Exception):
                failures.append(member["name"])
            else:
                results.append((member, response))
        if results:
            await ctx.send(self._format_team_stats(results))
        if failures:
            await ctx.send("Could not fetch stats for: " + ", ".join(f"`{name}`" for name in failures))

    @dayz_group.command(name="group")
    async def dayz_group_stats(self, ctx: commands.Context, server: str, *, group_name: str):
        """Show public Aftermath stats for a group on a monitored server.

        Example: [p]dayz group main Old Guys Gaming
        """
        server_id = await self._get_aftermath_server_id(ctx, server)
        if server_id is None:
            return
        try:
            stats = await self._fetch_aftermath_group_stats(server_id, group_name)
        except Exception as exc:
            await ctx.send(f"Could not fetch Aftermath group stats: `{exc}`")
            return
        if not stats.get("clan_name"):
            await ctx.send(f"No group stats found for `{group_name}`.")
            return
        await ctx.send(self._format_aftermath_stats(stats["clan_name"], stats))

    @dayz_group.command(name="player", aliases=["stats"])
    async def dayz_player_stats(self, ctx: commands.Context, server: str, steam_id: str):
        """Show public Aftermath stats for a Steam ID on a monitored server.

        Example: [p]dayz player main 76561198080332488
        """
        if not re.fullmatch(r"\d{17}", steam_id):
            await ctx.send("Steam ID must be a 17-digit SteamID64.")
            return
        server_id = await self._get_aftermath_server_id(ctx, server)
        if server_id is None:
            return
        try:
            stats = await self._fetch_aftermath_player_stats(server_id, steam_id)
        except Exception as exc:
            await ctx.send(f"Could not fetch Aftermath player stats: `{exc}`")
            return
        await ctx.send(self._format_aftermath_stats(f"Steam ID {steam_id}", stats))

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
            parsed = await self._fetch_status(address, trace=True)
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
                parsed = await self._fetch_status(address, trace=True)
                blocks.append(self._format_status(server.get("name", key), address, parsed))
            except Exception as exc:
                blocks.append(f"**{server.get('name', key)}** (`{address}`)\nError: `{exc}`")

        await ctx.send("\n\n".join(blocks[:10]))
