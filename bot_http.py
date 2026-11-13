"""The small HTTP server the website's backend calls to ask about Discord roles.

It listens where the backend's container can reach it, so anyone on the network could reach it too. Until now
it answered everyone. With DISCORD_BOT_SHARED_SECRET set (the same value as on the backend), a request must
carry `Authorization: Bearer <secret>` or it is refused. Without the variable it keeps answering everyone, so
that this file can be deployed before the secret is configured; a warning is logged once at startup.

Kept apart from index.py (which starts the bot when imported) so it can be tested on its own.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

SECRET_ENV = "DISCORD_BOT_SHARED_SECRET"
DEFAULT_PORT = 6965
MAX_ROLE_IDS = 20

HasRole = Callable[[int, int, int], Awaitable[bool]]


def _as_id(value) -> Optional[int]:
    """Discord ids arrive as strings or numbers; anything else is not an id."""
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def create_app(has_role: HasRole, *, shared_secret: Optional[str], allowed_guild_id: Optional[int]) -> web.Application:
    """`has_role(guild_id, user_id, role_id)` is the bot's own lookup; it may raise for unknown members."""

    @web.middleware
    async def require_secret(request: web.Request, handler):
        if shared_secret:
            presented = request.headers.get("Authorization", "")
            if not hmac.compare_digest(presented.encode(), f"Bearer {shared_secret}".encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def check_role(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "expected JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "expected an object"}, status=400)
        guild_id, user_id = _as_id(data.get("guild_id")), _as_id(data.get("user_id"))
        role_ids = data.get("role_ids")
        if guild_id is None or user_id is None or not isinstance(role_ids, list) or len(role_ids) > MAX_ROLE_IDS:
            return web.json_response({"error": "guild_id, user_id and role_ids are required"}, status=400)
        # Only ever answer about our own server.
        if allowed_guild_id is not None and guild_id != allowed_guild_id:
            return web.json_response({"has_role": False})
        for role_id in filter(None, map(_as_id, role_ids)):
            try:
                if await has_role(guild_id, user_id, role_id):
                    return web.json_response({"has_role": True})
            except Exception as error:
                # Someone who is not in the server has no roles; that is an answer, not a failure.
                logger.debug("role lookup for %s gave %s", user_id, type(error).__name__)
                return web.json_response({"has_role": False})
        return web.json_response({"has_role": False})

    app = web.Application(middlewares=[require_secret])
    app.router.add_post("/check_role", check_role)
    return app


async def start_server(has_role: HasRole, *, allowed_guild_id: Optional[int], host: str = "0.0.0.0",
                       port: int = DEFAULT_PORT) -> web.AppRunner:
    secret = os.environ.get(SECRET_ENV) or None
    if not secret:
        logger.warning("%s is not set: the role-check endpoint answers anyone who can reach port %s", SECRET_ENV, port)
    runner = web.AppRunner(create_app(has_role, shared_secret=secret, allowed_guild_id=allowed_guild_id))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
