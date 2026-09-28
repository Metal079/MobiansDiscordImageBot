import ast
import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord
from PIL import Image, PngImagePlugin
import psycopg

import image_metadata as m


def png(info=None):
    chunks = PngImagePlugin.PngInfo()
    for key, value in (info or {}).items():
        chunks.add_text(key, str(value))
    buffer = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buffer, "PNG", pnginfo=chunks)
    return buffer.getvalue()


def row(**changes):
    return dict(hash=-123, prompt="Make the jacket red.", negative_prompt="", seed=42, cfg=1,
                model="FLUX.2-klein-4B", loras="", created_date="2026-09-27",
                edit_provenance={"instruction": "Make the jacket red.", "steps": 4}, **changes)


class MetadataTests(unittest.TestCase):
    def test_backend_png_reports_edit_recipe(self):
        data, image_hash = m.read_image_metadata(png({
            "Disclaimer": "Mobians", "model": "Mobians.ai / FLUX.2-klein-4B",
            "prompt": "Make the jacket red.", "job_type": "instruction_edit", "seed": "0", "cfg": "1",
            "edit_provenance": json.dumps({"instruction": "Make the jacket red.", "steps": 4, "seed": 0}),
        }))
        self.assertIsNone(image_hash)
        self.assertEqual(data["Edit Instruction"], "Make the jacket red.")
        self.assertEqual(data["Edit Seed"], 0)
        self.assertNotIn("Original Prompt", data)
        self.assertTrue({"Edit CFG", "Edit Steps", "Note"}.isdisjoint(data))

    def test_history_download_preserves_both_recipes_without_exposing_ids(self):
        data = m.embedded_metadata({
            "Disclaimer": "Mobians", "model": "Mobians.ai / original-model", "prompt": "A blue fox.", "seed": "5",
            "edit_provenance": json.dumps({"instruction": "Make the jacket red.", "model": "FLUX.2-klein-4B",
                "seed": 42, "steps": 4, "guidance_scale": 1, "parent_image_uuid": "private", "reference_images": ["private"]}),
        })
        self.assertEqual(data["Original Prompt"], "A blue fox.")
        self.assertEqual(data["Original Seed"], "5")
        self.assertEqual(data["Edit Seed"], 42)
        self.assertEqual(data["Edit Model"], "FLUX.2-klein-4B")
        self.assertNotIn("private", str(data))

    def test_edit_detected_without_disclaimer_and_with_broken_provenance(self):
        for provenance in (None, "{bad json", "[]", "null"):
            with self.subTest(provenance=provenance):
                data = m.embedded_metadata({"model": "FLUX.2-klein-4B", "prompt": "Edit me", "edit_provenance": provenance})
                self.assertEqual(data["Edit Instruction"], "Edit me")

    def test_regular_metadata_formats_still_work(self):
        for info in (
            {"Disclaimer": "Mobians", "prompt": "original"},
            {"request_id": "123", "prompt": "original"},
            {"parameters-json": '{"PositivePrompt": "original"}'},
            {"parameters": "original\nNegative prompt: bad\nSteps: 20, CFG scale: 7"},
            {"parameters": "original"},
            {"invokeai": '{"Prompt": "original"}'},
            {"prompt": "original"},
        ):
            with self.subTest(info=info):
                self.assertEqual(m.embedded_metadata(info)["Prompt"], "original")

    def test_stripped_png_requests_visual_hash_lookup(self):
        metadata, image_hash = m.read_image_metadata(png())
        self.assertIsNone(metadata)
        self.assertIsInstance(image_hash, int)
        self.assertGreaterEqual(image_hash, -(1 << 63))
        self.assertLess(image_hash, 1 << 63)

    def test_invalid_image_is_rejected(self):
        with self.assertRaises(OSError):
            m.read_image_metadata(b"not an image")

    def test_closest_match_uses_edit_instruction(self):
        edited = row()
        original = {**row(), "hash": -124, "prompt": "Original prompt", "model": "novaMobianXL_v20", "edit_provenance": None}
        data = m.matched_metadata([original, edited], -123)
        self.assertEqual(data["Edit Instruction"], edited["prompt"])
        self.assertNotIn("Original Prompt", data)
        self.assertTrue({"Edit CFG", "Edit Steps", "Note"}.isdisjoint(data))

    def test_conflicting_matches_do_not_guess_the_prompt(self):
        a = row()
        b = {**a, "prompt": "A different image", "edit_provenance": None}
        self.assertIn("can't identify", m.matched_metadata([a, b], -123)["Result"])

    def test_duplicate_same_recipe_can_still_be_identified(self):
        self.assertIn("Edit Instruction", m.matched_metadata([row(), row()], -123))

    def test_legacy_hash_rows_and_no_match(self):
        regular = {**row(), "model": "novaMobianXL_v20", "edit_provenance": None}
        self.assertEqual(m.matched_metadata([regular], -123)["Prompt"], regular["prompt"])
        self.assertIn("original PNG", m.matched_metadata([], -123)["Result"])

    def test_long_edit_and_original_prompts_fit_discord_messages(self):
        instruction = "x" * 8000
        fields = {"Edit Instruction": instruction, "Original Prompt": "y" * 9000, "Seed": 42}
        embeds = m.metadata_embeds(fields, "https://example.com/image.png")
        self.assertGreater(len(embeds), 1)
        self.assertTrue(all(len(embed) <= 6000 and len(embed.fields) <= 25 for embed in embeds))
        self.assertTrue(all(len(field.value) <= 1024 for embed in embeds for field in embed.fields))
        self.assertEqual("".join(f.value for e in embeds for f in e.fields if f.name.startswith("Edit Instruction")), instruction)


class LookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_lookup_with_and_without_migration(self):
        for has_edits in (True, False):
            with self.subTest(has_edits=has_edits):
                cursor = AsyncMock()
                cursor.__aenter__.return_value = cursor
                cursor.fetchone.return_value = {"edit_table": "image_edit_hashes" if has_edits else None}
                cursor.fetchall.return_value = [row()]
                conn = AsyncMock()
                conn.__aenter__.return_value = conn
                conn.cursor = Mock(return_value=cursor)
                with patch.object(m.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)):
                    result = await m.lookup_hash_metadata(-123, "test-dsn")
                self.assertEqual(result["Edit Instruction"], "Make the jacket red.")
                query, params = cursor.execute.call_args.args
                self.assertEqual("UNION ALL" in query, has_edits)
                self.assertEqual(params, [-123, 1, -123, 1, -123] if has_edits else [-123, 1, -123])


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load just the handler; importing index.py would connect the real Discord bot.
        tree = ast.parse((Path(__file__).resolve().parents[1] / "index.py").read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_info")
        handler.decorator_list = []
        self.lookup = AsyncMock(return_value={"Edit Instruction": "Looked up"})
        self.client = SimpleNamespace(session=None)
        namespace = dict(asyncio=asyncio, discord=discord, aiohttp=aiohttp, Image=Image, psycopg=psycopg,
            logging=Mock(), client=self.client, DSN="test-dsn", read_image_metadata=m.read_image_metadata,
            lookup_hash_metadata=self.lookup, metadata_embeds=m.metadata_embeds)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "index.py", "exec"), namespace)
        self.handler = namespace["get_info"]

    def response(self, raw, status=200):
        @asynccontextmanager
        async def get(*args, **kwargs):
            yield SimpleNamespace(status=status, read=AsyncMock(return_value=raw))
        self.client.session = SimpleNamespace(get=get)

    def context(self, attachment=True, slash=False):
        return SimpleNamespace(message=SimpleNamespace(attachments=[SimpleNamespace(url="https://example.com/a.png")] if attachment else []),
            interaction=object() if slash else None, defer=AsyncMock(), send=AsyncMock())

    async def test_prefix_attachment_and_slash_url_use_embedded_edit(self):
        for slash in (False, True):
            with self.subTest(slash=slash):
                self.response(png({"model": "FLUX.2-klein-4B", "prompt": "Red jacket"}))
                ctx = self.context(attachment=not slash, slash=slash)
                await self.handler(ctx, url="https://example.com/a.png" if slash else None)
                self.assertEqual(ctx.send.call_args.kwargs["embed"].fields[1].value, "Red jacket")
                self.assertEqual(ctx.defer.await_count, int(slash))
                self.lookup.assert_not_awaited()

    async def test_stripped_image_uses_database(self):
        self.response(png())
        ctx = self.context()
        await self.handler(ctx)
        self.lookup.assert_awaited_once()
        self.assertEqual(ctx.send.call_args.kwargs["embed"].fields[0].value, "Looked up")

    async def test_failed_download_returns_without_hash_lookup(self):
        self.response(b"", status=404)
        ctx = self.context()
        await self.handler(ctx)
        self.assertIn("404", ctx.send.call_args.args[0])
        self.lookup.assert_not_awaited()

    async def test_database_error_is_reported(self):
        self.response(png())
        self.lookup.side_effect = psycopg.OperationalError("unavailable")
        ctx = self.context()
        await self.handler(ctx)
        self.assertIn("temporarily unavailable", ctx.send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
