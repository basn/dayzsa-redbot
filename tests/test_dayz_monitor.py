from __future__ import annotations

import asyncio
import logging
import struct
import sys
import os
from types import ModuleType
from typing import Any, Dict, Optional

import pytest


def _install_redbot_stubs() -> None:
    def _identity_decorator(*dargs, **dkwargs):
        if dargs and callable(dargs[0]) and not dkwargs:
            return dargs[0]

        def _decorator(func):
            return func

        return _decorator

    def _group_decorator(*dargs, **dkwargs):
        def _decorator(func):
            def _group_wrapper(*fargs, **fkwargs):
                return func(*fargs, **fkwargs)

            _group_wrapper.command = _identity_decorator
            _group_wrapper.group = _group_decorator
            return _group_wrapper

        if dargs and callable(dargs[0]) and not dkwargs:
            return _decorator(dargs[0])
        return _decorator

    if "redbot" not in sys.modules:
        redbot = ModuleType("redbot")
        core = ModuleType("redbot.core")
        bot = ModuleType("redbot.core.bot")
        commands = ModuleType("redbot.core.commands")
        utils = ModuleType("redbot.core.utils")
        chat_formatting = ModuleType("redbot.core.utils.chat_formatting")

        class Red:
            ...

        bot.Red = Red
        core.commands = commands
        core.Config = None
        core.bot = bot
        core.utils = utils
        redbot.core = core

        sys.modules.update(
            {
                "redbot": redbot,
                "redbot.core": core,
                "redbot.core.bot": bot,
                "redbot.core.commands": commands,
                "redbot.core.utils": utils,
                "redbot.core.utils.chat_formatting": chat_formatting,
            }
        )

    if "aiohttp" not in sys.modules:
        aiohttp = ModuleType("aiohttp")
        sys.modules["aiohttp"] = aiohttp

    if "discord" not in sys.modules:
        sys.modules["discord"] = ModuleType("discord")

    redbot = sys.modules["redbot"]
    core = sys.modules["redbot.core"]
    bot = sys.modules["redbot.core.bot"]
    commands = sys.modules["redbot.core.commands"]
    utils = sys.modules["redbot.core.utils"]
    chat_formatting = sys.modules["redbot.core.utils.chat_formatting"]
    aiohttp = sys.modules["aiohttp"]
    discord = sys.modules["discord"]

    if not hasattr(redbot, "core"):
        redbot.core = core
    if not hasattr(commands, "Cog"):
        class Cog:
            ...

        commands.Cog = Cog
    if not hasattr(bot, "Red"):
        class Red:
            ...

        bot.Red = Red

    if not hasattr(commands, "command"):
        commands.command = _identity_decorator
    else:
        commands.command = _identity_decorator
    if not hasattr(commands, "group"):
        commands.group = _group_decorator
    else:
        commands.group = _group_decorator
    if not hasattr(commands, "guild_only"):
        def guild_only():
            return _identity_decorator
    else:
        def guild_only():
            return _identity_decorator
    commands.guild_only = guild_only
    commands.admin_or_permissions = _identity_decorator

    if not hasattr(commands, "Context"):
        class Context:
            ...
        commands.Context = Context

    if not hasattr(core, "Config"):
        class Config:
            ...
            @classmethod
            def get_conf(cls, *args, **kwargs):
                return cls()
            def register_guild(self, *args, **kwargs):
                ...
            def guild(self, _guild):
                return self
            def servers(self):
                return {}
            def set(self, _data):
                return None
        core.Config = Config

    if not hasattr(utils, "chat_formatting"):
        utils.chat_formatting = chat_formatting
    if not hasattr(chat_formatting, "box"):
        def box(text: str, lang: Optional[str] = None) -> str:
            return text
        chat_formatting.box = box

    core.commands = commands
    core.bot = bot
    core.utils = utils
    utils.chat_formatting = chat_formatting

    if not hasattr(aiohttp, "ClientSession"):
        class ClientSession:
            def __init__(self, *args, **kwargs):
                ...
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def get(self, *args, **kwargs):
                raise NotImplementedError
            async def close(self):
                return None

        aiohttp.ClientSession = ClientSession

    if not hasattr(discord, "TextChannel"):
        discord.TextChannel = object
    if not hasattr(discord, "Guild"):
        discord.Guild = object


def _load_dayz_monitor_class():
    _install_redbot_stubs()
    from dayz_monitor.dayz_monitor import DayZMonitor

    return DayZMonitor


def _make_rules_payload(entries):
    def _cstring(value: str) -> bytes:
        return value.encode("utf-8") + b"\x00"

    payload = b"".join(_cstring(key) + _cstring(str(value)) for key, value in entries)
    return b"\xff\xff\xff\xffE" + struct.pack("<H", len(entries)) + payload


class _FakeTextChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.sent_messages = []

    async def send(self, content: str):
        self.sent_messages.append(content)


class _FakeGuild:
    def __init__(self, name: str, channel: Optional[_FakeTextChannel] = None, guild_id: int = 1):
        self.name = name
        self.id = guild_id
        self._channel = channel

    def get_channel(self, channel_id: int):
        if self._channel and self._channel.id == channel_id:
            return self._channel
        return None


class _FakeServersConfig:
    def __init__(self, data: Dict[str, Dict[str, Any]]):
        self.data = data
        self.set_calls = 0

    async def __call__(self):
        return self.data

    async def set(self, data: Dict[str, Dict[str, Any]]):
        self.set_calls += 1
        self.data = data


class _FakeGuildConfig:
    def __init__(self, data: Dict[str, Dict[str, Any]]):
        self.servers = _FakeServersConfig(data)


class _FakeConfig:
    def __init__(self, data: Dict[str, Dict[str, Any]]):
        self._servers_config = _FakeGuildConfig(data)

    def guild(self, _guild):
        return self._servers_config


def test_fetch_status_trace_logs_unresolved_queue(caplog):
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    async def fetch_server_data(_address: str):
        return {"players": 57, "maxPlayers": 100}

    async def fetch_a2s_info(_address: str, trace: bool = False):
        return {"online": None, "max_players": None, "queue": None}

    monitor._fetch_server_data = fetch_server_data  # type: ignore[attr-defined]
    monitor._fetch_a2s_info = fetch_a2s_info  # type: ignore[attr-defined]

    with caplog.at_level(logging.INFO):
        parsed = asyncio.run(monitor._fetch_status("127.0.0.1:27017", trace=True))

    assert parsed["queue"] is None
    assert any(
        "Queue still unresolved after API/A2S status resolution" in record.message
        for record in caplog.records
    )


def test_fetch_a2s_info_rules_fallback_queue_missing_logs(caplog):
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    call_count = 0

    async def fake_a2s_request(_host: str, _port: int, _payload: bytes):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("forced")
        return b"not-achallenge"

    monitor._a2s_request = fake_a2s_request  # type: ignore[attr-defined]
    monitor._parse_a2s_info = lambda _data: {
        "online": 10,
        "max_players": 20,
        "queue": None,
    }  # type: ignore[attr-defined]

    with caplog.at_level(logging.INFO):
        parsed = asyncio.run(monitor._fetch_a2s_info("127.0.0.1:27017", trace=True))

    assert parsed == {"online": None, "max_players": None, "queue": None}
    assert any(
        "A2S_RULES payload for 127.0.0.1:27017 did not include queue-related fields." in record.message
        for record in caplog.records
    )


def test_a2s_request_works_without_event_loop_udp_methods(monkeypatch):
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)
    module = sys.modules[DayZMonitor.__module__]
    calls = []

    class FakeSocket:
        def settimeout(self, timeout):
            calls.append(("timeout", timeout))

        def sendto(self, payload, address):
            calls.append(("sendto", payload, address))

        def recvfrom(self, size):
            calls.append(("recvfrom", size))
            return b"response", ("127.0.0.1", 27017)

        def close(self):
            calls.append(("close",))

    async def exercise():
        monkeypatch.setattr(module.socket, "socket", lambda *_args: FakeSocket())
        try:
            return await monitor._a2s_request("127.0.0.1", 27017, b"query", timeout=2, retries=1)
        finally:
            monkeypatch.undo()

    result = asyncio.run(exercise())

    assert result == b"response"
    assert calls == [
        ("timeout", 2),
        ("sendto", b"query", ("127.0.0.1", 27017)),
        ("recvfrom", 65535),
        ("close",),
    ]


def test_queue_parsers_cover_key_variants_and_bytes_matches():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    assert monitor._queue_from_keywords("lqs=17;waitingplayers:4") == 17
    assert monitor._queue_from_keywords("noise | queue:22;") == 22
    assert monitor._queue_from_bytes(b"prefix lqs:31,queueplayers=7") == 31
    assert monitor._queue_from_bytes(b"foo QUEUEPLAYERS:12 baz") == 12
    assert monitor._queue_from_bytes(b"nq  =  x") is None


def test_queue_from_rules_parses_known_fields_and_fallbacks():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    assert monitor._queue_from_rules(_make_rules_payload([("hostname", "x"), ("queue", "8"), ("lqs", "2")])) == 8
    assert monitor._queue_from_rules(_make_rules_payload([("waiting", "12")])) == 12
    assert monitor._queue_from_rules(_make_rules_payload([("note", "queueplayers:9")])) == 9
    assert monitor._queue_from_rules(_make_rules_payload([("note", "n/a")])) is None


def test_fetch_status_uses_api_values_when_complete():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    async def fetch_server_data(_address: str):
        return {"players": 50, "maxPlayers": 100, "queue": 3}

    called = {"a2s": 0}

    async def fetch_a2s_info(_address: str, trace: bool = False):
        called["a2s"] += 1
        return {"online": 1, "max_players": 2, "queue": 9}

    monitor._fetch_server_data = fetch_server_data  # type: ignore[attr-defined]
    monitor._fetch_a2s_info = fetch_a2s_info  # type: ignore[attr-defined]

    parsed = asyncio.run(monitor._fetch_status("127.0.0.1:27017"))

    assert parsed == {
        "online": 50,
        "max_players": 100,
        "queue": 3,
        "free_slots": 50,
        "is_full": False,
    }
    assert called["a2s"] == 0


def test_fetch_status_merges_queue_from_a2s_when_api_no_queue():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    async def fetch_server_data(_address: str):
        return {"players": 10, "maxplayers": 20}

    async def fetch_a2s_info(_address: str, trace: bool = False):
        return {"online": 8, "max_players": 20, "queue": 4}

    monitor._fetch_server_data = fetch_server_data  # type: ignore[attr-defined]
    monitor._fetch_a2s_info = fetch_a2s_info  # type: ignore[attr-defined]

    parsed = asyncio.run(monitor._fetch_status("127.0.0.1:27017"))

    assert parsed["online"] == 8
    assert parsed["max_players"] == 20
    assert parsed["queue"] == 4
    assert parsed["free_slots"] == 12
    assert parsed["is_full"] is False


def test_fetch_status_rethrows_api_failure_when_a2s_fails():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    async def fetch_server_data(_address: str):
        raise RuntimeError("api failure")

    async def fetch_a2s_info(_address: str, trace: bool = False):
        raise RuntimeError("a2s failure")

    monitor._fetch_server_data = fetch_server_data  # type: ignore[attr-defined]
    monitor._fetch_a2s_info = fetch_a2s_info  # type: ignore[attr-defined]

    try:
        asyncio.run(monitor._fetch_status("127.0.0.1:27017"))
    except RuntimeError as exc:
        assert "api failure" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError from API failure path")


def test_check_guild_sends_full_alert_once_with_queue_suffix():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    channel = _FakeTextChannel(100)
    guild = _FakeGuild("guild", channel)
    server_state = {
        "alpha": {
            "name": "alpha",
            "address": "127.0.0.1:27017",
            "channel_id": 100,
            "last_full": False,
            "not_full_since": None,
        }
    }
    monitor.config = _FakeConfig(server_state)

    statuses = [
        {"online": 20, "max_players": 20, "queue": 4, "free_slots": 0, "is_full": True},
        {"online": 20, "max_players": 20, "queue": 5, "free_slots": 0, "is_full": True},
    ]
    idx = 0

    async def fetch_status(_address: str, trace: bool = False):
        nonlocal idx
        value = statuses[idx]
        idx = min(idx + 1, len(statuses) - 1)
        return value

    monitor._fetch_status = fetch_status  # type: ignore[attr-defined]

    asyncio.run(monitor._check_guild(guild))
    asyncio.run(monitor._check_guild(guild))

    assert len(channel.sent_messages) == 1
    assert "is now full" in channel.sent_messages[0]
    assert "Queue: `4`" in channel.sent_messages[0]
    assert server_state["alpha"]["last_full"] is True
    assert server_state["alpha"]["not_full_since"] is None

    guild_config = monitor.config.guild(guild)
    assert guild_config.servers.set_calls == 1


def test_check_guild_short_flap_does_not_realert_full():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    channel = _FakeTextChannel(100)
    guild = _FakeGuild("guild", channel)
    server_state = {
        "alpha": {
            "name": "alpha",
            "address": "127.0.0.1:27017",
            "channel_id": 100,
            "last_full": False,
            "not_full_since": None,
        }
    }
    monitor.config = _FakeConfig(server_state)

    statuses = [
        {"online": 20, "max_players": 20, "queue": 4, "free_slots": 0, "is_full": True},
        {"online": 19, "max_players": 20, "queue": 4, "free_slots": 1, "is_full": False},
        {"online": 20, "max_players": 20, "queue": 4, "free_slots": 0, "is_full": True},
    ]
    idx = 0

    async def fetch_status(_address: str, trace: bool = False):
        nonlocal idx
        value = statuses[idx]
        idx = min(idx + 1, len(statuses) - 1)
        return value

    monitor._fetch_status = fetch_status  # type: ignore[attr-defined]

    asyncio.run(monitor._check_guild(guild))
    asyncio.run(monitor._check_guild(guild))
    asyncio.run(monitor._check_guild(guild))

    assert len(channel.sent_messages) == 1
    assert server_state["alpha"]["last_full"] is True
    assert server_state["alpha"]["not_full_since"] is None


def test_check_guild_rearms_full_state_after_non_full_ttl():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    channel = _FakeTextChannel(100)
    guild = _FakeGuild("guild", channel)
    server_state = {
        "alpha": {
            "name": "alpha",
            "address": "127.0.0.1:27017",
            "channel_id": 100,
            "last_full": True,
            "not_full_since": -1,
        }
    }
    monitor.config = _FakeConfig(server_state)
    async def fetch_status(_address: str, trace: bool = False):
        return {"online": 19, "max_players": 20, "queue": None, "free_slots": 1, "is_full": False}

    monitor._fetch_status = fetch_status  # type: ignore[attr-defined]
    asyncio.run(monitor._check_guild(guild))

    assert server_state["alpha"]["last_full"] is False
    assert server_state["alpha"]["not_full_since"] is None


def _live_server_address() -> str:
    return os.environ.get("DAYZ_MONITOR_LIVE_SERVER", "").strip()


@pytest.mark.skipif(not _live_server_address(), reason="Set DAYZ_MONITOR_LIVE_SERVER to run live integration checks.")
def test_fetch_status_live_against_real_server():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    address = _live_server_address()
    parsed = asyncio.run(monitor._fetch_status(address, trace=True))

    assert parsed["online"] is not None
    assert parsed["max_players"] is not None
    assert parsed["is_full"] is not None
    if parsed["online"] is not None and parsed["max_players"] is not None:
        assert isinstance(parsed["online"], int)
        assert isinstance(parsed["max_players"], int)
        assert parsed["online"] >= 0
        assert parsed["max_players"] >= 0

    if parsed["queue"] is not None:
        assert isinstance(parsed["queue"], int)
        assert parsed["queue"] >= 0


@pytest.mark.skipif(not _live_server_address(), reason="Set DAYZ_MONITOR_LIVE_SERVER to run live integration checks.")
def test_fetch_a2s_info_live_against_real_server():
    DayZMonitor = _load_dayz_monitor_class()
    monitor = DayZMonitor.__new__(DayZMonitor)

    address = _live_server_address()
    parsed = asyncio.run(monitor._fetch_a2s_info(address, trace=True))

    assert parsed["online"] is not None or parsed["max_players"] is not None or parsed["queue"] is not None
    if parsed["online"] is not None:
        assert isinstance(parsed["online"], int)
        assert parsed["online"] >= 0
    if parsed["max_players"] is not None:
        assert isinstance(parsed["max_players"], int)
        assert parsed["max_players"] >= 0
    if parsed["queue"] is not None:
        assert isinstance(parsed["queue"], int)
        assert parsed["queue"] >= 0
