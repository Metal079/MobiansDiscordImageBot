"""Read-only website/generation monitoring inside the existing Discord bot."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time

import psycopg
from psycopg.rows import dict_row
import requests


logger = logging.getLogger("mobians.generation_monitor")
POLL_SECONDS = 30
OUTAGE_SECONDS = 60
REMINDER_SECONDS = 1800
IMAGE_STALL_SECONDS = 600
IMAGE_JOB_SECONDS = 900
VIDEO_JOB_SECONDS = 1800
LABELS = {"website": "Website", "api": "Generation API", "database": "Queue monitoring",
          "images": "Image generation", "video": "Video generation"}


@dataclass
class Check:
    status: str
    reason: str
    # Idle traffic does not prove that a reported generation failure was fixed.
    last_success: float | None = None


def request_json(url):
    response = requests.get(url, timeout=(5, 10))
    response.raise_for_status()
    return response.json()


def website_health(url):
    try:
        response = requests.get(url, timeout=(5, 10))
        response.raise_for_status()
        if "<app-root" not in response.text.lower():
            return Check("unhealthy", "The website returned an unexpected page instead of the application")
        return Check("healthy", "The public website is responding normally")
    except requests.RequestException as exc:
        return Check("unhealthy", f"The public website is unreachable ({type(exc).__name__})")


def api_health(base):
    try:
        data = request_json(base + "/readiness_check")
        if data.get("database") != "ready":
            return Check("unhealthy", "The generation API did not confirm database readiness")
        return Check("healthy", "The public generation API and its database readiness check are responding normally")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        return Check("unhealthy", f"The generation API readiness check failed ({type(exc).__name__})")


def video_health(base):
    try:
        service = request_json(base + "/video/config")["service"]
        state = service["effective_state"]
        if state in {"disabled", "maintenance", "draining"}:
            return Check("paused", f"Video generation is intentionally {state}")
        if state == "available" and service.get("accepting_jobs") is True:
            return Check("healthy", "The video worker has a fresh heartbeat, reports healthy dependencies, and video submissions are enabled")
        age = service.get("heartbeat_age_seconds")
        if age is None or (isinstance(age, (int, float)) and age > 45):
            return Check("unhealthy", "The video worker heartbeat is missing or stale")
        return Check("unhealthy", f"Video worker reports {state}: {service.get('worker_message') or service.get('message') or 'Not accepting jobs'}")
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return Check("unhealthy", f"Video worker status could not be read from the API ({type(exc).__name__})")


IMAGE_SQL = """
SELECT q.id::text, q.status, q.model, q.job_type,
       EXTRACT(EPOCH FROM q.create_date)::float8 AS created,
       EXTRACT(EPOCH FROM q.last_updated_date AT TIME ZONE current_setting('TimeZone'))::float8 AS updated
FROM public.generation_queue q
WHERE q.is_dev_job IS NOT TRUE
  AND (q.status IN ('pending','processing') OR
       (q.status IN ('completed','failed') AND q.last_updated_date > LOCALTIMESTAMP - INTERVAL '15 minutes'))
ORDER BY q.last_updated_date DESC NULLS LAST
"""
VIDEO_SQL = """
SELECT id::text, status, EXTRACT(EPOCH FROM create_date)::float8 AS created,
       EXTRACT(EPOCH FROM started_at)::float8 AS started,
       EXTRACT(EPOCH FROM completed_at)::float8 AS completed
FROM public.video_generation_queue
WHERE is_dev_job IS NOT TRUE AND
      (status IN ('pending','processing') OR completed_at > NOW() - INTERVAL '15 minutes')
"""


def queue_snapshot(dsn):
    # The bot reuses its existing DB access; no generation, charging, or edits.
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True, row_factory=dict_row,
                         application_name="mobians_generation_monitor",
                         options="-c default_transaction_read_only=on -c statement_timeout=5000") as conn:
        clock_row = conn.execute("SELECT EXTRACT(EPOCH FROM NOW())::float8 AS now").fetchone()
        if clock_row is None:
            raise psycopg.DataError("Database clock query returned no result")
        now = clock_row["now"]
        images = conn.execute(IMAGE_SQL).fetchall()
        videos = conn.execute(VIDEO_SQL).fetchall()
    return now, images, videos


def image_health(rows, now):
    active = [row for row in rows if row["status"] in {"pending", "processing"}]
    recent = [row for row in rows if row["status"] in {"completed", "failed"}
              and row["updated"] is not None and now - row["updated"] < 600]
    completed = [row for row in recent if row["status"] == "completed"]
    last_success = max((row["updated"] for row in completed), default=None)
    problems = []
    stuck = [row for row in active if row["status"] == "processing"
             and now - (row["updated"] or row["created"]) > IMAGE_JOB_SECONDS]
    if stuck:
        problems.append(f"{len(stuck)} image job(s) have been processing for over {IMAGE_JOB_SECONDS // 60} minutes")
    if active and not completed and now - min(row["created"] for row in active) > IMAGE_STALL_SECONDS:
        problems.append(f"{len(active)} image job(s) are waiting/running with no successful completion in {IMAGE_STALL_SECONDS // 60} minutes")
    groups = defaultdict(list)
    for row in sorted(recent, key=lambda row: row["updated"], reverse=True):
        groups[(row["model"], row["job_type"])].append(row["status"])
    for (model, job_type), statuses in groups.items():
        failed = statuses.count("failed")
        if statuses[:3] == ["failed"] * 3 or (failed >= 5 and failed / len(statuses) >= .5):
            problems.append(f"{model}/{job_type}: {failed} of {len(statuses)} recent jobs failed")
    if problems:
        return Check("unhealthy", "; ".join(problems[:5]), last_success)
    if not active and not completed:
        return Check("idle", "No queued image jobs or recent results; waiting for demand")
    return Check("healthy", f"{len(completed)} image job(s) completed in the last 10 minutes; {len(active)} queued/running", last_success)


def video_queue_health(check, rows, now):
    if check.status != "healthy":
        return check
    processing = [row for row in rows if row["status"] == "processing"]
    pending = [row for row in rows if row["status"] == "pending"]
    overdue = [row for row in processing if now - (row["started"] or row["created"]) > VIDEO_JOB_SECONDS]
    if overdue:
        return Check("unhealthy", f"{len(overdue)} video job(s) have been processing for over {VIDEO_JOB_SECONDS // 60} minutes despite the worker heartbeat")
    if pending and not processing and now - min(row["created"] for row in pending) > 120:
        return Check("unhealthy", "Video jobs have been waiting over two minutes without being picked up by the worker")
    return check


def collect_checks(dsn, api_base="https://api.mobians.ai", website_url="https://mobians.ai"):
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            "website": executor.submit(website_health, website_url),
            "api": executor.submit(api_health, api_base),
            "video": executor.submit(video_health, api_base),
            "queues": executor.submit(queue_snapshot, dsn),
        }
        checks = {name: futures[name].result() for name in ("website", "api", "video")}
        try:
            now, images, videos = futures["queues"].result()
            checks["database"] = Check("healthy", "Generation queues can be read")
            checks["images"] = image_health(images, now)
            checks["video"] = video_queue_health(checks["video"], videos, now)
        except psycopg.Error as exc:
            checks["database"] = Check("unhealthy", f"The bot cannot inspect generation queues ({type(exc).__name__})")
            checks["images"] = Check("unknown", "Queue monitoring is unavailable")
        # A shared API outage gets one clear alert; video recovery is not inferred.
        if checks["api"].status == "unhealthy":
            checks["video"] = Check("unknown", "Video status is unavailable while the generation API is down")
    return checks


@dataclass
class Incident:
    failed_since: float | None = None
    alerted: bool = False
    last_alert_at: float = 0
    healthy_checks: int = 0
    reason: str = ""
    last_status: str = ""
    pause_notified: bool = False


@dataclass
class MonitorState:
    initialized: bool = False
    incidents: dict[str, Incident] = field(default_factory=dict)

    def observe(self, name: str, check: Check, now: float):
        incident = self.incidents.setdefault(name, Incident())
        if check.status != incident.last_status:
            incident.healthy_checks = 0
        incident.last_status = check.status
        if check.status in {"unknown", "idle"}:
            incident.healthy_checks = 0
            return None
        if check.status == "unhealthy":
            incident.pause_notified = False
            incident.healthy_checks = 0
            incident.reason = check.reason
            if incident.failed_since is None:
                incident.failed_since = now
                logger.error("%s: %s", LABELS[name], check.reason)
            if now - incident.failed_since < OUTAGE_SECONDS:
                return None
            if incident.alerted and now - incident.last_alert_at < REMINDER_SECONDS:
                return None
            since = datetime.fromtimestamp(incident.failed_since, timezone.utc).isoformat(timespec="seconds")
            heading = "STILL HAVING TROUBLE" if incident.alerted else "PROBLEM DETECTED"
            return ("outage", f"Mobians — {LABELS[name]} — {heading}\n{check.reason}\nFirst observed: {since}\nI will notify you when this check recovers.")
        if name == "images" and incident.failed_since is not None:
            if check.last_success is None or check.last_success < incident.failed_since:
                incident.healthy_checks = 0
                return None
        incident.healthy_checks += 1
        if incident.healthy_checks < 2:
            return None
        if not incident.alerted:
            self.incidents[name] = Incident()
            return None
        if check.status == "paused":
            if incident.pause_notified:
                return None
            return ("paused", f"Mobians — {LABELS[name]} — PLANNED PAUSE\n{check.reason}\nReminders paused. This is not a recovery confirmation.")
        elapsed = now - incident.failed_since if incident.failed_since is not None else 0
        minutes = max(0, int(elapsed / 60))
        return ("recovery", f"Mobians — {LABELS[name]} — BACK ONLINE\n{check.reason}\nIssue lasted about {minutes} minute(s). Confirmed on two consecutive checks.")

    def delivered(self, name, kind, now):
        if kind == "outage":
            self.incidents[name].alerted = True
            self.incidents[name].last_alert_at = now
        elif kind == "paused":
            self.incidents[name].pause_notified = True
        else:
            self.incidents[name] = Incident()


def load_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return MonitorState(initialized=data.get("initialized", False),
                            incidents={key: Incident(**value) for key, value in data.get("incidents", {}).items()})
    except FileNotFoundError:
        return MonitorState()
    except (OSError, ValueError, TypeError, AttributeError):
        logger.exception("Could not restore generation alert state; continuing with fresh state")
        return MonitorState()


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    temporary.replace(path)


async def run_bot_monitor(bot, dsn, *, recipient_id=143845908830748672, state_path=None):
    import discord

    await bot.wait_until_ready()
    path = Path(state_path or Path(__file__).with_name(".generation-monitor-state.json"))
    state = load_state(path)
    recipient = None
    while not bot.is_closed():
        try:
            checks = await asyncio.to_thread(collect_checks, dsn)
            now = time.time()
            if recipient is None:
                recipient = await bot.fetch_user(recipient_id)
            if not state.initialized:
                summary = "\n".join(f"{LABELS[name]}: {check.status}" for name, check in checks.items())
                try:
                    await recipient.send("Mobians monitoring enabled in the image info bot. I will DM you about website/API outages, stopped or failing image jobs, and video worker problems, then confirm recovery.\n\n" + summary,
                                         allowed_mentions=discord.AllowedMentions.none())
                    state.initialized = True
                except discord.HTTPException:
                    logger.exception("Monitoring-enabled DM failed; health checks will continue")
            for name, check in checks.items():
                notice = state.observe(name, check, now)
                if notice is None:
                    continue
                kind, message = notice
                try:
                    await recipient.send(message[:1900], allowed_mentions=discord.AllowedMentions.none())
                    state.delivered(name, kind, now)
                    logger.info("Operator DM delivered: %s %s", name, kind)
                except discord.HTTPException:
                    logger.exception("Operator DM failed: %s; retrying on next poll", name)
            await asyncio.to_thread(save_state, path, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Generation monitoring failed; retrying on next poll")
        await asyncio.sleep(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check without sending any Discord messages")
    args = parser.parse_args()
    if not args.once:
        parser.error("Run inside the existing image info bot, or use --once")
    import os
    dsn = psycopg.conninfo.make_conninfo(host=os.getenv("DBHOST"), dbname=os.getenv("DBNAME"),
                                       user=os.getenv("DBUSER"), password=os.getenv("DBPASS"))
    checks = collect_checks(dsn)
    print(json.dumps({name: asdict(check) for name, check in checks.items()}, indent=2))
    raise SystemExit(any(check.status == "unhealthy" for check in checks.values()))


if __name__ == "__main__":
    main()
