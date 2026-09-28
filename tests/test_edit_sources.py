import ast
import asyncio
import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from PIL import Image

import edit_sources as s

JOB_ID = "0d660d5f-637d-4b07-893d-3cd7b1dd5434"
OTHER_JOB_ID = "00000000-0000-4000-8000-000000000001"


def image_data(format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(buffer, format)
    return base64.b64encode(buffer.getvalue()).decode()


def result(**changes):
    return dict(job_id=JOB_ID, distance=0, image=image_data(), reference_images=[], **changes)


class SourceFilesTests(unittest.TestCase):
    def test_base_and_two_references_preserve_original_bytes_and_labels(self):
        base, ref1, ref2 = image_data(), image_data("JPEG"), image_data("WEBP")
        files, notes = s.source_files({"image": "data:image/png;base64," + base, "reference_images": [ref1, ref2]})
        self.assertEqual([f[0] for f in files], ["Base image", "Reference 1", "Reference 2"])
        self.assertEqual([f[2] for f in files], ["base-image.png", "reference-1.jpg", "reference-2.webp"])
        self.assertEqual(files[0][1], base64.b64decode(base))
        self.assertEqual(notes, [])

    def test_no_references_differs_from_expired_references(self):
        files, notes = s.source_files(result())
        self.assertEqual(len(files), 1)
        self.assertEqual(notes, ["No reference images were used."])
        files, notes = s.source_files({"image": None, "reference_images": None})
        self.assertFalse(files)
        self.assertIn("expire", notes[0])

    def test_partial_retention_returns_remaining_sources(self):
        files, notes = s.source_files({"image": None, "reference_images": [image_data()]})
        self.assertEqual([f[0] for f in files], ["Reference 1"])
        self.assertIn("Base image is no longer available.", notes)

    def test_invalid_and_oversized_sources_are_not_uploaded(self):
        for invalid in ("not base64!", base64.b64encode(b"not an image").decode(), "A" * (s.MAX_SOURCE_BYTES * 4 // 3 + 101)):
            with self.subTest(length=len(invalid)):
                files, notes = s.source_files({"image": invalid, "reference_images": []})
                self.assertFalse(files)
                self.assertIn("could not be read", " ".join(notes))

    def test_closest_unique_job_wins_but_identical_hash_jobs_are_ambiguous(self):
        exact = {"job_id": JOB_ID, "hash": -123}
        near = {"job_id": OTHER_JOB_ID, "hash": -124}
        self.assertEqual(s.select_edit_job([near, exact], -123), (JOB_ID, 0))
        with self.assertRaisesRegex(s.SourceLookupError, "Several edit jobs"):
            s.select_edit_job([exact, {**near, "hash": -123}], -123)
        with self.assertRaisesRegex(s.SourceLookupError, "No saved edit"):
            s.select_edit_job([], -123)


class SourceLookupTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, one_results, all_results=None):
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchone.side_effect = one_results
        cursor.fetchall.return_value = all_results or []
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.cursor = Mock(return_value=cursor)
        return connection, cursor

    async def test_hash_lookup_retrieves_queue_inputs_using_saved_job_id(self):
        saved = {"image": image_data(), "reference_images": [image_data()]}
        conn, cursor = self.connection([{"edit_table": "image_edit_hashes"}, saved], [{"job_id": JOB_ID, "hash": -123}])
        with patch.object(s.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)):
            found = await s.lookup_edit_sources("dsn", image_hash=-123)
        self.assertEqual(found["job_id"], JOB_ID)
        self.assertEqual(found["reference_images"], saved["reference_images"])
        self.assertIn("SET TRANSACTION READ ONLY", cursor.execute.call_args_list[0].args[0])
        query, params = cursor.execute.call_args.args
        self.assertIn("public.generation_queue", query)
        self.assertEqual(params[0], JOB_ID)

    async def test_expired_queue_job_reports_retention(self):
        conn, cursor = self.connection([None])
        with patch.object(s.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)):
            with self.assertRaisesRegex(s.SourceLookupError, "expire"):
                await s.lookup_edit_sources("dsn", job_id=JOB_ID)
        self.assertFalse(any("image_edit_hashes" in call.args[0] for call in cursor.execute.call_args_list))

    async def test_ambiguous_hash_never_fetches_source_pixels(self):
        conn, cursor = self.connection([{"edit_table": "image_edit_hashes"}], [
            {"job_id": JOB_ID, "hash": -123}, {"job_id": OTHER_JOB_ID, "hash": -123}])
        with patch.object(s.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)):
            with self.assertRaises(s.SourceLookupError):
                await s.lookup_edit_sources("dsn", image_hash=-123)
        self.assertFalse(any("generation_queue" in call.args[0] for call in cursor.execute.call_args_list))


class CommandAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "index.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in ("_is_mod_or_admin", "get_sources")]
        for node in nodes:
            node.decorator_list = []
        self.send = AsyncMock()
        namespace = dict(discord=discord, MODERATION_GUILD_ID=s.MODERATION_GUILD_ID,
            send_edit_sources=self.send, client=SimpleNamespace(session="session"), DSN="dsn")
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "index.py", "exec"), namespace)
        self.handler = namespace["get_sources"]

    def ctx(self, role=None, guild=s.MODERATION_GUILD_ID, admin=False, slash=False):
        return SimpleNamespace(guild=SimpleNamespace(id=guild) if guild else None,
            author=SimpleNamespace(roles=[SimpleNamespace(name=role)] if role else [],
                                   guild_permissions=SimpleNamespace(administrator=admin)),
            interaction=object() if slash else None, send=AsyncMock())

    async def test_non_mods_other_guilds_and_dms_never_enter_source_handler(self):
        for ctx in (self.ctx(), self.ctx(role="Mod", guild=99), self.ctx(role="Mod", guild=None), self.ctx(slash=True)):
            await self.handler(ctx, "https://example.com/edit.png")
            self.assertIn("only available", ctx.send.call_args.args[0])
        self.send.assert_not_awaited()

    async def test_mods_and_admins_can_use_prefix_or_slash(self):
        for ctx in (self.ctx(role="Mod"), self.ctx(admin=True), self.ctx(role="Mod", slash=True)):
            await self.handler(ctx, JOB_ID)
        self.assertEqual(self.send.await_count, 3)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, slash=False, attachment=True):
        return SimpleNamespace(interaction=object() if slash else None, defer=AsyncMock(), send=AsyncMock(),
            message=SimpleNamespace(attachments=[SimpleNamespace(url="https://example.com/edit.png")] if attachment else []),
            author=SimpleNamespace(send=AsyncMock()))

    async def test_prefix_sources_are_posted_in_the_invoking_channel(self):
        ctx = self.ctx()
        captured = []
        async def capture(content, **kwargs):
            if "file" in kwargs:
                captured.append((content, kwargs["file"].filename, kwargs["file"].fp.read()))
        ctx.send.side_effect = capture
        with patch.object(s, "download_edit_hash", AsyncMock(return_value=-123)), \
             patch.object(s, "lookup_edit_sources", AsyncMock(return_value={**result(), "reference_images": [image_data()]})):
            await s.send_edit_sources(ctx, "session", "dsn")
        self.assertEqual([r[0] for r in captured], ["Base image", "Reference 1"])
        self.assertTrue(all(r[2] for r in captured))
        self.assertEqual(ctx.send.await_count, 3)
        self.assertTrue(all(not call.kwargs["ephemeral"] for call in ctx.send.call_args_list))
        ctx.author.send.assert_not_awaited()

    async def test_slash_sources_are_all_ephemeral(self):
        ctx = self.ctx(slash=True)
        with patch.object(s, "download_edit_hash", AsyncMock(return_value=-123)), \
             patch.object(s, "lookup_edit_sources", AsyncMock(return_value=result())):
            await s.send_edit_sources(ctx, "session", "dsn")
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(all(call.kwargs["ephemeral"] for call in ctx.send.call_args_list))
        ctx.author.send.assert_not_awaited()

    async def test_explicit_job_id_skips_download(self):
        ctx = self.ctx(attachment=False)
        with patch.object(s, "download_edit_hash", AsyncMock()) as download, \
             patch.object(s, "lookup_edit_sources", AsyncMock(return_value=result())) as lookup:
            await s.send_edit_sources(ctx, "session", "dsn", JOB_ID)
        download.assert_not_awaited()
        lookup.assert_awaited_once_with("dsn", image_hash=None, job_id=JOB_ID)

    async def test_closed_dms_do_not_affect_channel_delivery(self):
        ctx = self.ctx()
        ctx.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "DMs closed")
        with patch.object(s, "download_edit_hash", AsyncMock(return_value=-123)), \
             patch.object(s, "lookup_edit_sources", AsyncMock(return_value=result())):
            await s.send_edit_sources(ctx, "session", "dsn")
        self.assertTrue(any("file" in call.kwargs for call in ctx.send.call_args_list))
        ctx.author.send.assert_not_awaited()

    async def test_prefix_expired_sources_message_is_posted_in_chat(self):
        ctx = self.ctx()
        with patch.object(s, "download_edit_hash", AsyncMock(return_value=-123)), \
             patch.object(s, "lookup_edit_sources", AsyncMock(side_effect=s.SourceLookupError("Sources expired"))):
            await s.send_edit_sources(ctx, "session", "dsn")
        self.assertEqual(ctx.send.call_args.args[0], "Sources expired")
        self.assertFalse(ctx.send.call_args.kwargs["ephemeral"])
        ctx.author.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
