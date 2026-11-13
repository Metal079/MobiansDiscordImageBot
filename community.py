"""What the bot does for members of the server: a welcome, the daily bonus and balance from inside Discord,
the join reward, and a record of how the server's size changes.

Everything here works on the website's own database and rules:
  * /daily calls claim_daily_bonus, the same function the website calls, so amounts and streaks cannot differ;
  * the join reward calls claim_discord_join_reward, which pays once per website account and Discord account;
  * member counts go to discord_member_stats (website migration 068), one row per site day.
A Discord user is tied to a website account only by having connected Discord on the site (users.discord_user_id).

Kept apart from index.py (which starts the bot when imported) so it can be tested without Discord: database
access goes through a `connect` callable that returns an async psycopg connection.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SITE_URL = "https://mobians.ai"
SITE_TIME_ZONE = "America/Chicago"      # the website's day (daily bonus, statistics) turns over on this clock
WELCOME_BURST_LIMIT = 5                 # more joins than this within a minute looks like a raid: stay quiet
WELCOME_BURST_SECONDS = 60

Connect = Callable[[], Awaitable[Any]]

MEMBER_STATS_SQL = f"""
    INSERT INTO discord_member_stats AS stats (day, member_count, joins, leaves)
    VALUES ((NOW() AT TIME ZONE '{SITE_TIME_ZONE}')::date, %(count)s, %(joins)s, %(leaves)s)
    ON CONFLICT (day) DO UPDATE SET
        member_count = COALESCE(EXCLUDED.member_count, stats.member_count),
        joins = stats.joins + EXCLUDED.joins,
        leaves = stats.leaves + EXCLUDED.leaves,
        updated_at = NOW()
"""

ACCOUNT_SQL = """
    SELECT id, credits, COALESCE(daily_bonus_streak, 0), COALESCE(is_banned, FALSE),
           (last_daily_bonus IS NULL OR last_daily_bonus <> CURRENT_DATE) AS can_claim,
           CASE WHEN last_daily_bonus = CURRENT_DATE OR last_daily_bonus = CURRENT_DATE - 1
                THEN LEAST(30 + COALESCE(daily_bonus_streak, 0) * 10, 50) ELSE 30 END AS next_bonus
    FROM users WHERE discord_user_id = %s
"""


class WelcomeLimiter:
    """Lets welcomes through at a human pace and goes quiet during a flood of joins."""

    def __init__(self, limit: int = WELCOME_BURST_LIMIT, seconds: float = WELCOME_BURST_SECONDS, clock=time.monotonic):
        self._limit, self._seconds, self._clock, self._recent = limit, seconds, clock, deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._recent and now - self._recent[0] > self._seconds:
            self._recent.popleft()
        self._recent.append(now)
        return len(self._recent) <= self._limit


# --- wording ------------------------------------------------------------------------------------------

def welcome_message(mention: str, reward: Optional[dict]) -> str:
    """`reward` is the result of grant_join_reward: None when this member has no connected website account."""
    lines = [f"Welcome to Mobians, {mention}!"]
    if reward and reward.get("granted"):
        lines.append(f"Your Discord is connected to your Mobians account, so **{reward['credits']} credits** were just added "
                     f"(balance: {reward['new_balance']}).")
    elif reward is None:
        lines.append(f"Connect this Discord account at {SITE_URL} (Account panel, **Join our Discord**) and get **100 credits**, once.")
    lines.append("Use `/daily` here to claim your daily bonus and `/credits` to see your balance.")
    return "\n".join(lines)


def not_connected_message() -> str:
    return (f"Your Discord account isn't connected to a Mobians account yet. Sign in at {SITE_URL} with Discord, or open the "
            "Account panel there and choose **Connect Discord**. Then try again.")


def credits_message(account: Optional[dict]) -> str:
    if account is None:
        return not_connected_message()
    if account["banned"]:
        return "This account is banned."
    streak = account["streak"]
    lines = [f"You have **{account['credits']} credits**."]
    if account["can_claim"]:
        lines.append(f"Today's bonus is ready: `/daily` for **+{account['next_bonus']}**."
                     + (f" You're on a {streak}-day streak." if streak > 0 and account["next_bonus"] > 30 else ""))
    else:
        lines.append(f"Daily bonus claimed today. Streak: {streak} day{'s' if streak != 1 else ''}.")
    return "\n".join(lines)


def daily_message(result: Optional[dict]) -> str:
    if result is None:
        return not_connected_message()
    if result.get("banned"):
        return "This account is banned."
    if result.get("unavailable"):
        return f"The daily bonus couldn't be claimed right now. Try again in a minute, or claim it at {SITE_URL}."
    if result["success"]:
        streak = result["streak"]
        return (f"**+{result['credits_awarded']} credits** claimed. Balance: {result['new_balance']}. "
                f"Streak: {streak} day{'s' if streak != 1 else ''}. Come back tomorrow to keep it going.")
    return f"{result['message']}. Balance: {result['new_balance']}."


# --- database -------------------------------------------------------------------------------------------

async def record_member_change(connect: Connect, *, member_count: Optional[int], joins: int = 0, leaves: int = 0) -> bool:
    """Add to today's row of discord_member_stats. Returns False (and logs) when it could not be written."""
    try:
        async with await connect() as connection:
            await connection.execute(MEMBER_STATS_SQL, {"count": member_count, "joins": joins, "leaves": leaves})
            await connection.commit()
        return True
    except Exception as error:
        logger.warning("member statistics not recorded: %s", error)
        return False


async def find_account(connect: Connect, discord_user_id: int | str) -> Optional[dict]:
    """The website account connected to this Discord user, or None."""
    async with await connect() as connection:
        cursor = await connection.execute(ACCOUNT_SQL, (str(discord_user_id),))
        row = await cursor.fetchone()
    if row is None:
        return None
    return {"user_id": row[0], "credits": row[1], "streak": row[2], "banned": bool(row[3]),
            "can_claim": bool(row[4]), "next_bonus": row[5]}


async def claim_daily(connect: Connect, discord_user_id: int | str) -> Optional[dict]:
    """Claim today's bonus for the connected account, through the website's own function."""
    try:
        async with await connect() as connection:
            cursor = await connection.execute(
                "SELECT id, COALESCE(is_banned, FALSE) FROM users WHERE discord_user_id = %s", (str(discord_user_id),))
            account = await cursor.fetchone()
            if account is None:
                return None
            if account[1]:
                return {"banned": True}
            cursor = await connection.execute("SELECT * FROM claim_daily_bonus(%s)", (account[0],))
            success, credits_awarded, new_balance, streak, message = await cursor.fetchone()
            await connection.commit()
    except Exception as error:
        logger.warning("daily bonus from Discord failed: %s", error)
        return {"unavailable": True}
    return {"success": bool(success), "credits_awarded": credits_awarded, "new_balance": new_balance,
            "streak": streak, "message": message}


async def grant_join_reward(connect: Connect, discord_user_id: int | str) -> Optional[dict]:
    """Pay the one-time join reward if this Discord user already has a connected website account.

    None means there is no connected account (the welcome then explains how to get the reward).
    A result with granted=False means it was already paid, or could not be paid right now.
    """
    try:
        async with await connect() as connection:
            cursor = await connection.execute(
                "SELECT id FROM users WHERE discord_user_id = %s AND NOT COALESCE(is_banned, FALSE)", (str(discord_user_id),))
            account = await cursor.fetchone()
            if account is None:
                return None
            cursor = await connection.execute(
                "SELECT * FROM claim_discord_join_reward(%s, %s, %s)", (account[0], str(discord_user_id), "bot_join"))
            granted, credits, new_balance = await cursor.fetchone()
            await connection.commit()
    except Exception as error:
        logger.warning("join reward from Discord failed: %s", error)
        return {"granted": False, "credits": 0, "new_balance": None}
    return {"granted": bool(granted), "credits": credits, "new_balance": new_balance}


# --- events ---------------------------------------------------------------------------------------------

async def on_member_join(connect: Connect, *, discord_user_id: int, mention: str, member_count: Optional[int],
                         send_welcome: Optional[Callable[[str], Awaitable[Any]]], limiter: WelcomeLimiter) -> None:
    """Count the join, pay the reward if their account is already connected, and say hello if a channel is set."""
    await record_member_change(connect, member_count=member_count, joins=1)
    reward = await grant_join_reward(connect, discord_user_id)
    if send_welcome is None or not limiter.allow():
        return
    try:
        await send_welcome(welcome_message(mention, reward))
    except Exception as error:
        logger.warning("welcome message not sent: %s", error)


async def on_member_remove(connect: Connect, *, member_count: Optional[int]) -> None:
    await record_member_change(connect, member_count=member_count, leaves=1)
