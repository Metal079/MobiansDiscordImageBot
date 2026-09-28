"""Moderator-only retrieval of temporary edit inputs; never persist image data."""
import asyncio
import base64
import binascii
from io import BytesIO
import logging
from urllib.parse import urlparse
from uuid import UUID

import aiohttp
import discord
from PIL import Image
import psycopg
from psycopg.rows import dict_row

from image_metadata import EDIT_MODELS, signed_image_hash

MODERATION_GUILD_ID = 1095514548112461924
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class SourceLookupError(ValueError):
    """A lookup result that can be shown to the requesting moderator."""


def select_edit_job(rows, image_hash):
    if not rows:
        raise SourceLookupError("No saved edit matches this image. Try the original edited PNG or its generation job ID.")
    distance = lambda row: bin((int(row["hash"]) ^ image_hash) & ((1 << 64) - 1)).count("1")
    best_distance = min(map(distance, rows))
    jobs = list(dict.fromkeys(str(row["job_id"]) for row in rows if distance(row) == best_distance))
    if len(rows) >= 101 or len(jobs) != 1:
        candidates = "\n".join(jobs[:5])
        raise SourceLookupError(
            "Several edit jobs match this image; I won't choose sources from an uncertain match. "
            "If you know the correct job, use !getsources <job ID>. Matching candidates:\n" + candidates
        )
    return jobs[0], best_distance


async def lookup_edit_sources(dsn, *, image_hash=None, job_id=None):
    """Call only after checking moderator access. The queue owns retention."""
    distance = None
    if job_id is not None:
        job_id = str(UUID(str(job_id)))
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10, row_factory=dict_row) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SET TRANSACTION READ ONLY")
            await cursor.execute("SET LOCAL statement_timeout = '10s'")
            if job_id is None:
                if image_hash is None:
                    raise SourceLookupError("Provide an edited image or its generation job ID.")
                await cursor.execute("SELECT to_regclass('public.image_edit_hashes') AS edit_table")
                if (await cursor.fetchone())["edit_table"] is None:
                    raise SourceLookupError("Edited-image lookup is not configured yet. A generation job ID can still be used.")
                await cursor.execute("""
                    SELECT job_id, hash FROM public.image_edit_hashes
                    WHERE hash <@ (%s, %s)
                    ORDER BY (hash = %s) DESC, created_date DESC LIMIT 101
                """, (image_hash, 1, image_hash))
                job_id, distance = select_edit_job(await cursor.fetchall(), image_hash)
            await cursor.execute("""
                SELECT image, edit_input->'reference_images' AS reference_images
                FROM public.generation_queue
                WHERE id = %s AND model = ANY(%s) AND status = 'completed'
            """, (job_id, list(EDIT_MODELS)))
            row = await cursor.fetchone()
    if row is None:
        raise SourceLookupError("That edit job is no longer available or wasn't found. Older source images expire from the generation queue.")
    return {"job_id": job_id, "distance": distance, **row}


def source_files(result):
    """Validate in memory and keep each original file below the editor's upload limit."""
    references = result.get("reference_images")
    inputs = [("Base image", result.get("image"))]
    notes = []
    if isinstance(references, list):
        if not references:
            notes.append("No reference images were used.")
        inputs += [(f"Reference {index}", value) for index, value in enumerate(references[:2], 1)]
    else:
        notes.append("Reference images are no longer available.")
    files = []
    for label, encoded in inputs:
        if not encoded:
            notes.append(f"{label} is no longer available.")
            continue
        try:
            if not isinstance(encoded, str) or len(encoded) > MAX_SOURCE_BYTES * 4 // 3 + 100:
                raise ValueError("Invalid source size")
            raw = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
            if not raw or len(raw) > MAX_SOURCE_BYTES:
                raise ValueError("Invalid source size")
            with Image.open(BytesIO(raw)) as image:
                extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
                if extension is None or image.width * image.height > 16_000_000:
                    raise ValueError("Invalid source image")
                image.verify()
            filename = label.lower().replace(" ", "-") + "." + extension
            files.append((label, raw, filename))
        except (ValueError, TypeError, binascii.Error, OSError, Image.DecompressionBombError):
            notes.append(f"{label} could not be read from the saved job.")
    if not files:
        notes.insert(0, "The source images are no longer saved or could not be read. Older jobs expire from the generation queue.")
    return files, notes


def hash_image_bytes(raw):
    with Image.open(BytesIO(raw)) as image:
        return signed_image_hash(image)


async def download_edit_hash(session, url):
    url = url.strip("<>")
    if urlparse(url).scheme not in ("https", "http"):
        raise SourceLookupError("Provide an image URL, an attachment, or a valid generation job ID.")
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status != 200:
            raise SourceLookupError(f"Couldn't download the edited image (HTTP {response.status}).")
        raw = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            raw.extend(chunk)
            if len(raw) > MAX_DOWNLOAD_BYTES:
                raise SourceLookupError("The edited image is too large. Use its generation job ID instead.")
    return await asyncio.to_thread(hash_image_bytes, raw)


async def send_edit_sources(ctx, session, dsn, url=None):
    """The command wrapper enforces guild and Mod/admin access before entering here."""
    slash = bool(ctx.interaction)
    if slash:
        await ctx.defer(ephemeral=True)

    async def send_private(content, **kwargs):
        kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        if slash:
            return await ctx.send(content, ephemeral=True, **kwargs)
        return await ctx.author.send(content, **kwargs)

    try:
        attachments = getattr(ctx.message, "attachments", [])
        image_url = attachments[0].url if attachments else url
        if not image_url:
            raise SourceLookupError("Attach an edited image or use !getsources <image URL or job ID>.")
        try:
            job_id = str(UUID(image_url)) if not attachments else None
        except ValueError:
            job_id = None
        image_hash = None if job_id else await download_edit_hash(session, image_url)
        result = await lookup_edit_sources(dsn, image_hash=image_hash, job_id=job_id)
        files, notes = await asyncio.to_thread(source_files, result)
        heading = f"Edit sources — job `{result['job_id']}`"
        if result.get("distance"):
            heading += "\nPossible match by visual hash; please verify the images."
        if notes:
            heading += "\n" + "\n".join(notes)
        await send_private(heading)
        # Send separately so three large inputs do not exceed a message upload limit.
        for label, raw, filename in files:
            with BytesIO(raw) as buffer:
                file = discord.File(buffer, filename=filename, description=label)
                try:
                    await send_private(label, file=file)
                finally:
                    file.close()
        if not slash:
            await ctx.send("Sent the edit-source lookup to your DMs.")
    except SourceLookupError as exc:
        await _send_lookup_error(ctx, send_private, str(exc))
    except (psycopg.Error, aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, Image.DecompressionBombError):
        logging.exception("Edit source lookup failed")
        await _send_lookup_error(ctx, send_private, "I couldn't retrieve the edit sources right now. Try again or use the generation job ID.")
    except discord.HTTPException:
        await ctx.send("I couldn't deliver the source images privately. Enable DMs or try /getsources.", ephemeral=slash)


async def _send_lookup_error(ctx, send_private, message):
    try:
        await send_private(message)
        if not ctx.interaction:
            await ctx.send("Sent the edit-source lookup result to your DMs.")
    except discord.HTTPException:
        await ctx.send("I couldn't DM you. Enable DMs or try /getsources.", ephemeral=bool(ctx.interaction))
