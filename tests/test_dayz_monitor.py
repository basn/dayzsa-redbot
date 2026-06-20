from __future__ import annotations

import asyncio
import logging
import sys
from types import ModuleType
from typing import Any, Dict, Optional


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
    assert any("A2S_RULES payload for 127.0.0.1:27017 did not include queue-related fields." in record.message for record in caplog.records)
