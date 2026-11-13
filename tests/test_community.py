import unittest

import community as c

DISCORD_ID = 222333444555666777
ACCOUNT_ID = "0d660d5f-637d-4b07-893d-3cd7b1dd5434"


class FakeDatabase:
    """Stands in for the website's database: answers each statement from a list of (text it contains, row)."""

    def __init__(self, answers=(), fail_on=None):
        self.answers, self.fail_on = list(answers), fail_on
        self.statements, self.commits, self.opened = [], 0, 0

    def connect(self):
        database = self

        class Cursor:
            def __init__(self, row):
                self.row = row

            async def fetchone(self):
                return self.row

        class Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def execute(self, sql, params=None):
                database.statements.append((" ".join(sql.split()), params))
                if database.fail_on and database.fail_on in sql:
                    raise RuntimeError("database unavailable")
                for fragment, row in database.answers:
                    if fragment in sql:
                        return Cursor(row)
                return Cursor(None)

            async def commit(self):
                database.commits += 1

        async def open_connection():
            database.opened += 1
            return Connection()

        return open_connection()

    def ran(self, fragment):
        return [params for sql, params in self.statements if fragment in sql]


class WelcomeLimiterTests(unittest.TestCase):
    def test_goes_quiet_during_a_flood_of_joins_and_recovers_afterwards(self):
        now = [0.0]
        limiter = c.WelcomeLimiter(limit=3, seconds=60, clock=lambda: now[0])
        self.assertEqual([limiter.allow() for _ in range(5)], [True, True, True, False, False])
        now[0] = 30
        self.assertFalse(limiter.allow())                      # still inside the same minute
        now[0] = 200
        self.assertTrue(limiter.allow())                       # the flood has passed


class WordingTests(unittest.TestCase):
    def test_welcome_for_a_member_without_a_connected_account_explains_the_reward(self):
        message = c.welcome_message("<@1>", None)
        self.assertIn("Welcome to Mobians, <@1>!", message)
        self.assertIn("100 credits", message)
        self.assertIn(c.SITE_URL, message)
        self.assertIn("/daily", message)

    def test_welcome_for_a_connected_member_says_what_was_added(self):
        message = c.welcome_message("<@1>", {"granted": True, "credits": 100, "new_balance": 340})
        self.assertIn("**100 credits** were just added (balance: 340)", message)
        self.assertNotIn("Connect this Discord account", message)

    def test_welcome_for_someone_already_rewarded_promises_nothing(self):
        message = c.welcome_message("<@1>", {"granted": False, "credits": 0, "new_balance": 340})
        self.assertNotIn("100", message)
        self.assertIn("/credits", message)

    def test_credits_reply(self):
        account = {"credits": 250, "streak": 2, "banned": False, "can_claim": True, "next_bonus": 50}
        self.assertEqual(c.credits_message(account),
                         "You have **250 credits**.\nToday's bonus is ready: `/daily` for **+50**. You're on a 2-day streak.")
        self.assertEqual(c.credits_message({**account, "streak": 5, "next_bonus": 30}),      # streak lapsed: no streak boast
                         "You have **250 credits**.\nToday's bonus is ready: `/daily` for **+30**.")
        self.assertEqual(c.credits_message({**account, "can_claim": False, "streak": 1}),
                         "You have **250 credits**.\nDaily bonus claimed today. Streak: 1 day.")
        self.assertEqual(c.credits_message({**account, "banned": True}), "This account is banned.")
        self.assertIn("isn't connected", c.credits_message(None))

    def test_daily_reply(self):
        claimed = {"success": True, "credits_awarded": 40, "new_balance": 290, "streak": 2, "message": "Daily bonus claimed!"}
        self.assertEqual(c.daily_message(claimed),
                         "**+40 credits** claimed. Balance: 290. Streak: 2 days. Come back tomorrow to keep it going.")
        again = {"success": False, "credits_awarded": 0, "new_balance": 290, "streak": 2, "message": "Daily bonus already claimed today"}
        self.assertEqual(c.daily_message(again), "Daily bonus already claimed today. Balance: 290.")
        self.assertIn("isn't connected", c.daily_message(None))
        self.assertEqual(c.daily_message({"banned": True}), "This account is banned.")
        self.assertIn("couldn't be claimed right now", c.daily_message({"unavailable": True}))


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_changes_add_to_todays_row(self):
        database = FakeDatabase()
        self.assertTrue(await c.record_member_change(database.connect, member_count=812, joins=1))
        self.assertEqual(database.ran("INSERT INTO discord_member_stats"), [{"count": 812, "joins": 1, "leaves": 0}])
        self.assertEqual(database.commits, 1)

    async def test_a_missing_statistics_table_is_logged_and_never_stops_the_bot(self):
        database = FakeDatabase(fail_on="discord_member_stats")
        with self.assertLogs(c.logger, "WARNING"):
            self.assertFalse(await c.record_member_change(database.connect, member_count=812, leaves=1))

    async def test_find_account(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID, 250, 2, False, True, 50))])
        self.assertEqual(await c.find_account(database.connect, DISCORD_ID),
                         {"user_id": ACCOUNT_ID, "credits": 250, "streak": 2, "banned": False, "can_claim": True, "next_bonus": 50})
        self.assertEqual(database.statements[0][1], (str(DISCORD_ID),))          # ids are stored as text on the website
        self.assertIsNone(await c.find_account(FakeDatabase().connect, DISCORD_ID))

    async def test_daily_goes_through_the_websites_own_function(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID, False)),
                                 ("claim_daily_bonus", (True, 30, 280, 1, "Daily bonus claimed!"))])
        self.assertEqual(await c.claim_daily(database.connect, DISCORD_ID),
                         {"success": True, "credits_awarded": 30, "new_balance": 280, "streak": 1, "message": "Daily bonus claimed!"})
        self.assertEqual(database.ran("claim_daily_bonus"), [(ACCOUNT_ID,)])
        self.assertEqual(database.commits, 1)

    async def test_daily_for_unconnected_and_banned_members_claims_nothing(self):
        unconnected = FakeDatabase()
        self.assertIsNone(await c.claim_daily(unconnected.connect, DISCORD_ID))
        banned = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID, True))])
        self.assertEqual(await c.claim_daily(banned.connect, DISCORD_ID), {"banned": True})
        for database in (unconnected, banned):
            self.assertEqual(database.ran("claim_daily_bonus"), [])
            self.assertEqual(database.commits, 0)

    async def test_daily_reports_a_database_problem_instead_of_raising(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID, False))], fail_on="claim_daily_bonus")
        with self.assertLogs(c.logger, "WARNING"):
            self.assertEqual(await c.claim_daily(database.connect, DISCORD_ID), {"unavailable": True})

    async def test_join_reward_is_paid_through_the_websites_function_for_a_connected_account(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID,)),
                                 ("claim_discord_join_reward", (True, 100, 340))])
        self.assertEqual(await c.grant_join_reward(database.connect, DISCORD_ID), {"granted": True, "credits": 100, "new_balance": 340})
        self.assertEqual(database.ran("claim_discord_join_reward"), [(ACCOUNT_ID, str(DISCORD_ID), "bot_join")])
        self.assertIn("NOT COALESCE(is_banned, FALSE)", database.statements[0][0])      # banned accounts are not rewarded
        self.assertEqual(database.commits, 1)

    async def test_join_reward_without_a_connected_account_is_none(self):
        database = FakeDatabase()
        self.assertIsNone(await c.grant_join_reward(database.connect, DISCORD_ID))
        self.assertEqual(database.ran("claim_discord_join_reward"), [])

    async def test_join_reward_problem_is_not_reported_as_unconnected(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID,))], fail_on="claim_discord_join_reward")
        with self.assertLogs(c.logger, "WARNING"):
            reward = await c.grant_join_reward(database.connect, DISCORD_ID)
        self.assertEqual(reward, {"granted": False, "credits": 0, "new_balance": None})
        self.assertNotIn("Connect this Discord account", c.welcome_message("<@1>", reward))


class MemberEventTests(unittest.IsolatedAsyncioTestCase):
    async def join(self, database, *, send="record", limiter=None):
        sent = []

        async def record(message):
            sent.append(message)

        async def broken(message):
            raise RuntimeError("missing permission")

        send_welcome = {"record": record, "broken": broken, None: None}[send]
        await c.on_member_join(database.connect, discord_user_id=DISCORD_ID, mention="<@1>", member_count=812,
                               send_welcome=send_welcome, limiter=limiter or c.WelcomeLimiter())
        return sent

    async def test_a_join_is_counted_rewarded_and_welcomed(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID,)), ("claim_discord_join_reward", (True, 100, 340))])
        sent = await self.join(database)
        self.assertEqual(database.ran("INSERT INTO discord_member_stats"), [{"count": 812, "joins": 1, "leaves": 0}])
        self.assertEqual(len(sent), 1)
        self.assertIn("**100 credits** were just added", sent[0])

    async def test_without_a_welcome_channel_the_join_is_still_counted_and_rewarded(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID,)), ("claim_discord_join_reward", (True, 100, 340))])
        self.assertEqual(await self.join(database, send=None), [])
        self.assertEqual(len(database.ran("INSERT INTO discord_member_stats")), 1)
        self.assertEqual(len(database.ran("claim_discord_join_reward")), 1)

    async def test_during_a_flood_joins_are_counted_and_rewarded_but_not_announced(self):
        database = FakeDatabase()
        limiter = c.WelcomeLimiter(limit=2, seconds=60, clock=lambda: 0.0)
        sent = []
        for _ in range(4):
            sent += await self.join(database, limiter=limiter)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(database.ran("INSERT INTO discord_member_stats")), 4)

    async def test_statistics_or_channel_problems_never_escape_the_event(self):
        database = FakeDatabase([("FROM users WHERE discord_user_id", (ACCOUNT_ID,)), ("claim_discord_join_reward", (True, 100, 340))],
                                fail_on="discord_member_stats")
        with self.assertLogs(c.logger, "WARNING") as logs:
            await self.join(database, send="broken")
        self.assertEqual(len(database.ran("claim_discord_join_reward")), 1)       # the reward did not depend on the statistics
        self.assertTrue(any("welcome message not sent" in line for line in logs.output))

    async def test_a_leave_is_counted(self):
        database = FakeDatabase()
        await c.on_member_remove(database.connect, member_count=811)
        self.assertEqual(database.ran("INSERT INTO discord_member_stats"), [{"count": 811, "joins": 0, "leaves": 1}])


if __name__ == "__main__":
    unittest.main()
