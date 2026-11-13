import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import bot_http

GUILD = 1095514548112461924
SECRET = "a-long-shared-secret"


class RoleCheckServerTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, *, secret=None, roles=(), error=None, guild=GUILD):
        """Start the server with a stand-in for the bot's lookup; `roles` are the role ids the member has."""
        self.lookups = []

        async def has_role(guild_id, user_id, role_id):
            self.lookups.append((guild_id, user_id, role_id))
            if error:
                raise error
            return role_id in roles

        client = TestClient(TestServer(bot_http.create_app(has_role, shared_secret=secret, allowed_guild_id=guild)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def ask(self, client, body=None, **kwargs):
        body = {"guild_id": str(GUILD), "user_id": "42", "role_ids": ["7", "8"]} if body is None else body
        response = await client.post("/check_role", json=body, **kwargs)
        return response.status, await response.json()

    # --- who may ask ---

    async def test_with_a_secret_set_requests_without_it_are_refused_before_any_lookup(self):
        client = await self.serve(secret=SECRET, roles={7})
        self.assertEqual(await self.ask(client), (401, {"error": "unauthorized"}))
        for wrong in ("Bearer nope", SECRET, f"bearer {SECRET}", f"Bearer {SECRET} ", "Bearer YourSecretToken"):
            self.assertEqual((await self.ask(client, headers={"Authorization": wrong}))[0], 401, wrong)
        self.assertEqual(self.lookups, [])

    async def test_with_the_right_secret_it_answers(self):
        client = await self.serve(secret=SECRET, roles={8})
        self.assertEqual(await self.ask(client, headers={"Authorization": f"Bearer {SECRET}"}), (200, {"has_role": True}))

    async def test_without_a_secret_configured_it_still_answers_so_it_can_ship_before_the_secret(self):
        client = await self.serve(secret=None, roles={7})
        self.assertEqual(await self.ask(client), (200, {"has_role": True}))
        # The placeholder header the backend has always sent keeps working until the secret is set.
        self.assertEqual(await self.ask(client, headers={"Authorization": "Bearer YourSecretToken"}), (200, {"has_role": True}))

    async def test_start_server_reads_the_secret_from_the_environment_and_warns_when_missing(self):
        async def has_role(*_):
            return True

        with mock.patch.dict("os.environ", {bot_http.SECRET_ENV: ""}), self.assertLogs(bot_http.logger, "WARNING") as logs:
            runner = await bot_http.start_server(has_role, allowed_guild_id=GUILD, host="127.0.0.1", port=0)
            await runner.cleanup()
        self.assertIn("answers anyone", logs.output[0])

    # --- what it answers ---

    async def test_any_one_of_the_roles_is_enough_and_ids_may_be_text_or_numbers(self):
        client = await self.serve(roles={8})
        self.assertEqual(await self.ask(client, {"guild_id": GUILD, "user_id": 42, "role_ids": [7, "8", 9]}), (200, {"has_role": True}))
        self.assertEqual(self.lookups, [(GUILD, 42, 7), (GUILD, 42, 8)])      # stops at the first match

    async def test_no_matching_role_is_false(self):
        client = await self.serve(roles={99})
        self.assertEqual(await self.ask(client), (200, {"has_role": False}))

    async def test_someone_who_is_not_in_the_server_has_no_roles_rather_than_an_error(self):
        client = await self.serve(error=LookupError("Unknown Member"))
        self.assertEqual(await self.ask(client), (200, {"has_role": False}))

    async def test_it_only_answers_about_its_own_server(self):
        client = await self.serve(roles={7})
        self.assertEqual(await self.ask(client, {"guild_id": "123", "user_id": "42", "role_ids": ["7"]}), (200, {"has_role": False}))
        self.assertEqual(self.lookups, [])

    async def test_malformed_requests_are_refused_without_a_lookup(self):
        client = await self.serve(roles={7})
        bad = [
            [],                                                                  # not an object
            {"user_id": "42", "role_ids": ["7"]},                                # no server
            {"guild_id": str(GUILD), "role_ids": ["7"]},                         # no member
            {"guild_id": str(GUILD), "user_id": "42"},                           # no roles
            {"guild_id": str(GUILD), "user_id": "42", "role_ids": "7"},          # roles not a list
            {"guild_id": str(GUILD), "user_id": "abc", "role_ids": ["7"]},       # not an id
            {"guild_id": str(GUILD), "user_id": "-5", "role_ids": ["7"]},
            {"guild_id": str(GUILD), "user_id": "42", "role_ids": list(range(1, bot_http.MAX_ROLE_IDS + 2))},
        ]
        for body in bad:
            self.assertEqual((await self.ask(client, body))[0], 400, body)
        response = await client.post("/check_role", data="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.lookups, [])

    async def test_role_ids_that_are_not_ids_are_skipped(self):
        client = await self.serve(roles={8})
        self.assertEqual(await self.ask(client, {"guild_id": GUILD, "user_id": 42, "role_ids": [None, "x", {}, 8]}), (200, {"has_role": True}))
        self.assertEqual(self.lookups, [(GUILD, 42, 8)])


if __name__ == "__main__":
    unittest.main()
