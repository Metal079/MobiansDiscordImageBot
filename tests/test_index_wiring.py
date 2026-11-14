"""index.py itself: the wiring between discord.py and the bot's modules.

index.py starts the bot when it is run, so it is loaded here without its last line (`client.run(token)`).
These tests exist because a helper named `connect` on the bot class once replaced discord.py's own `connect`
(the method that takes the bot online): every other test passed and the bot could not start.
"""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands

import community

INDEX = Path(__file__).resolve().parent.parent / "index.py"
# What MyBot is meant to replace in discord.py's Bot. Anything else with a discord.py name is an accident.
INTENDED_OVERRIDES = {"__init__", "setup_hook", "close"}


def load_index() -> dict:
    source = INDEX.read_text(encoding="utf-8")
    body, run, rest = source.rpartition("client.run(token)")
    assert run and not rest.strip(), "index.py is expected to end with client.run(token)"
    namespace = {"__name__": "index_under_test", "__file__": str(INDEX)}
    exec(compile(body, str(INDEX), "exec"), namespace)
    return namespace


class WiringTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load_index()
        cls.client = cls.index["client"]
        cls.guild_id = cls.index["GUILD_ID"]

    def member(self, *, guild_id=None, bot=False):
        return SimpleNamespace(id=42, mention="<@42>", bot=bot,
                               guild=SimpleNamespace(id=self.guild_id if guild_id is None else guild_id, member_count=812))

    # --- starting ---

    async def test_the_bot_goes_online_through_discord_pys_own_connect(self):
        with patch.object(discord.Client, "login", new=AsyncMock()) as login, \
                patch.object(discord.Client, "connect", new=AsyncMock()) as connect:
            await self.client.start("not-a-real-token")
        login.assert_awaited_once_with("not-a-real-token")
        connect.assert_awaited_once_with(reconnect=True)

    def test_the_bot_class_replaces_only_what_it_means_to(self):
        bot_class = self.index["MyBot"]
        accidents = sorted(name for name in vars(bot_class)
                           if name not in INTENDED_OVERRIDES and not name.startswith("__") and hasattr(commands.Bot, name))
        self.assertEqual(accidents, [])

    async def test_startup_starts_the_role_check_server_once_and_shutdown_stops_it(self):
        client = self.index["MyBot"]()
        runner = SimpleNamespace(cleanup=AsyncMock())
        start_server = AsyncMock(return_value=runner)
        not_ready_yet = asyncio.Event()                  # stands in for "logged in, waiting for Discord's READY"
        with patch.object(client.tree, "sync", new=AsyncMock()) as sync, \
                patch.dict(self.index, {"start_server": start_server, "run_bot_monitor": AsyncMock()}), \
                patch.object(client, "wait_until_ready", new=AsyncMock(side_effect=not_ready_yet.wait)), \
                patch.object(discord.Client, "close", new=AsyncMock()):
            await client.setup_hook()
            await client.on_ready()                      # a reconnect: must not start a second server
            await client.on_ready()
            await asyncio.sleep(0)
            self.assertTrue(client.member_count_snapshot.is_running())
            await client.close()
        sync.assert_awaited_once()
        start_server.assert_awaited_once_with(client.has_role, allowed_guild_id=self.guild_id)
        runner.cleanup.assert_awaited_once()
        self.assertTrue(client.session.closed)

    # --- database helper ---

    def test_the_database_helper_opens_a_connection_with_the_bots_settings(self):
        with patch("psycopg.AsyncConnection.connect", new=MagicMock(return_value="a connection")) as connect:
            self.assertEqual(self.client.open_database(), "a connection")
        connect.assert_called_once_with(self.index["DSN"])

    # --- members ---

    async def test_a_new_member_is_handed_to_community_with_the_database_helper(self):
        with patch.object(community, "on_member_join", new=AsyncMock()) as joined:
            await self.client.on_member_join(self.member())
        joined.assert_awaited_once()
        args, kwargs = joined.await_args
        self.assertEqual(args, (self.client.open_database,))
        self.assertEqual({key: kwargs[key] for key in ("discord_user_id", "mention", "member_count")},
                         {"discord_user_id": 42, "mention": "<@42>", "member_count": 812})
        self.assertIs(kwargs["limiter"], self.client.welcome_limiter)
        self.assertIsNone(kwargs["send_welcome"])           # no WELCOME_CHANNEL_ID: counted and rewarded, not announced

    async def test_other_servers_and_bots_are_ignored(self):
        with patch.object(community, "on_member_join", new=AsyncMock()) as joined, \
                patch.object(community, "on_member_remove", new=AsyncMock()) as left:
            await self.client.on_member_join(self.member(guild_id=1))
            await self.client.on_member_join(self.member(bot=True))
            await self.client.on_member_remove(self.member(guild_id=1))
            await self.client.on_member_remove(self.member(bot=True))
        joined.assert_not_awaited()
        left.assert_not_awaited()

    async def test_a_leaving_member_is_counted(self):
        with patch.object(community, "on_member_remove", new=AsyncMock()) as left:
            await self.client.on_member_remove(self.member())
        left.assert_awaited_once_with(self.client.open_database, member_count=812)

    async def test_the_periodic_snapshot_records_the_servers_size(self):
        snapshot = self.index["MyBot"].member_count_snapshot.coro
        with patch.object(community, "record_member_change", new=AsyncMock()) as record:
            with patch.object(self.client, "get_guild", return_value=SimpleNamespace(member_count=901)) as get_guild:
                await snapshot(self.client)
            get_guild.assert_called_once_with(self.guild_id)
            record.assert_awaited_once_with(self.client.open_database, member_count=901)
            with patch.object(self.client, "get_guild", return_value=None):       # not in the cache yet: skip, never fail
                await snapshot(self.client)
            record.assert_awaited_once()

    # --- slash commands ---

    def interaction(self):
        return SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_credits_answers_privately_from_the_websites_database(self):
        interaction = self.interaction()
        account = {"credits": 250, "streak": 0, "banned": False, "can_claim": True, "next_bonus": 30}
        with patch.object(community, "find_account", new=AsyncMock(return_value=account)) as find:
            await self.index["credits_command"].callback(interaction)
        find.assert_awaited_once_with(self.client.open_database, 42)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(community.credits_message(account), ephemeral=True)

    async def test_credits_says_so_when_the_database_cannot_be_reached(self):
        interaction = self.interaction()
        with patch.object(community, "find_account", new=AsyncMock(side_effect=OSError("down"))), self.assertLogs(level="ERROR"):
            await self.index["credits_command"].callback(interaction)
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("try again", message)

    async def test_daily_answers_privately_through_the_websites_claim(self):
        interaction = self.interaction()
        with patch.object(community, "claim_daily", new=AsyncMock(return_value=None)) as claim:
            await self.index["daily_command"].callback(interaction)
        claim.assert_awaited_once_with(self.client.open_database, 42)
        interaction.followup.send.assert_awaited_once_with(community.not_connected_message(), ephemeral=True)

    def test_both_member_commands_are_registered_for_the_server(self):
        registered = {command.name for command in self.client.tree.get_commands(guild=discord.Object(id=self.guild_id))}
        self.assertTrue({"credits", "daily"} <= registered, registered)


if __name__ == "__main__":
    unittest.main()
