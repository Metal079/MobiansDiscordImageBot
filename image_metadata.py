"""Read shared image metadata, including Mobians instruction edits."""
import json
from io import BytesIO

import discord
import imagehash
import psycopg
from PIL import Image
from psycopg.rows import dict_row


EDIT_MODELS = ("FLUX.2-klein-4B", "FLUX.2-klein-9B", "Qwen-Image-2.1")


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def edit_metadata(info, provenance):
    instruction = provenance.get("instruction") or info.get("prompt", "N/A")
    metadata = {
        "Generation Type": "Image edit",
        "Edit Instruction": instruction,
        "Edit Model": provenance.get("model") or info.get("model", "N/A"),
        "Edit Seed": provenance.get("seed", info.get("seed", "N/A")),
        "Edit CFG": provenance.get("guidance_scale", info.get("cfg", "N/A")),
    }
    if provenance.get("steps") is not None:
        metadata["Edit Steps"] = provenance["steps"]
    if provenance.get("width") and provenance.get("height"):
        metadata["Edit Dimensions"] = f"{provenance['width']} × {provenance['height']}"
    # History downloads can retain the source recipe alongside edit_provenance.
    if info.get("prompt") and info["prompt"] != instruction:
        for label, key in (("Original Prompt", "prompt"), ("Original Negative Prompt", "negative_prompt"),
                           ("Original Model", "model"), ("Original Seed", "seed"),
                           ("Original CFG", "cfg"), ("Original LoRAs", "loras")):
            if info.get(key) is not None:
                metadata[label] = info[key]
    metadata["Note"] = "Recreating an edit also requires its source image and any reference images."
    return metadata


def embedded_metadata(info):
    """Return display fields, or None when a database lookup is needed."""
    provenance = _object(info.get("edit_provenance"))
    model = str(info.get("model", "")).removeprefix("Mobians.ai / ")
    if provenance.get("instruction") or info.get("job_type") == "instruction_edit" or model in EDIT_MODELS:
        return edit_metadata(info, provenance)
    if "Disclaimer" in info:
        return {label: info.get(key, "N/A") for label, key in (
            ("Prompt", "prompt"), ("Negative Prompt", "negative_prompt"), ("Seed", "seed"),
            ("Cfg", "cfg"), ("Model", "model"), ("loras", "loras"))}
    if "request_id" in info:
        return {label: info.get(key, "N/A") for label, key in (
            ("Prompt", "prompt"), ("Loras", "loras"), ("Cfg", "CFG"), ("Steps", "steps"),
            ("Generation Type", "model_type"), ("Generation Date", "generation_date"))}
    if "parameters-json" in info:
        data = _object(info["parameters-json"])
        if data:
            return {label: data.get(key, "N/A") for label, key in (
                ("Prompt", "PositivePrompt"), ("Negative Prompt", "NegativePrompt"),
                ("Seed", "Seed"), ("Cfg", "CfgScale"), ("Model", "ModelName"))}
    if "parameters" in info:
        lines = str(info["parameters"]).splitlines()
        if lines:
            metadata = {"Prompt": lines[0]}
            rest = lines[1:]
            if rest and rest[0].startswith("Negative prompt:"):
                metadata["Negative Prompt"] = rest.pop(0).removeprefix("Negative prompt:").strip()
            if rest:
                metadata["Misc Info"] = "\n".join(rest)
            return metadata
    if "invokeai" in info:
        data = _object(info["invokeai"])
        if data:
            return data
    if "prompt" in info:
        return {
            "Prompt": info["prompt"], "Negative Prompt": info.get("negative_prompt", "N/A"),
            "Seed": info.get("seed", "N/A"), "Cfg": info.get("guidance_scale", info.get("cfg", "N/A")),
            "Model": str(info.get("model") or info.get("use_stable_diffusion_model", "Unknown model")).split("stable-diffusion")[-1],
        }
    return None


def signed_image_hash(image):
    value = int(str(imagehash.phash(image, hash_size=8)), 16)
    return value - (1 << 64) if value & (1 << 63) else value


def read_image_metadata(raw):
    with Image.open(BytesIO(raw)) as image:
        image.load()
        metadata = embedded_metadata(image.info)
        return metadata, signed_image_hash(image) if metadata is None else None


async def lookup_hash_metadata(image_hash, dsn):
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10, row_factory=dict_row) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SET LOCAL statement_timeout = '10s'")
            # The bot can be upgraded before the backend migration is deployed.
            await cursor.execute("SELECT to_regclass('public.image_edit_hashes') AS edit_table")
            has_edits = (await cursor.fetchone())["edit_table"] is not None
            query = """
                SELECT hash, prompt, negative_prompt, seed, cfg, model, created_date, loras,
                       NULL::jsonb AS edit_provenance
                FROM public.hashes WHERE hash <@ (%s, %s)
            """
            params = [image_hash, 1]
            if has_edits:
                query += """
                    UNION ALL
                    SELECT hash, instruction AS prompt, '' AS negative_prompt, seed, cfg,
                           model, created_date, '' AS loras, edit_provenance
                    FROM public.image_edit_hashes WHERE hash <@ (%s, %s)
                """
                params += [image_hash, 1]
            # Sort exact matches first, with a bounded result set for repeated images.
            query = "SELECT * FROM (" + query + ") AS matches ORDER BY (hash = %s) DESC, created_date DESC LIMIT 101"
            await cursor.execute(query, params + [image_hash])
            rows = await cursor.fetchall()
    return matched_metadata(rows, image_hash)


def matched_metadata(rows, image_hash):
    if not rows:
        return {"Result": "No saved prompt was found. Try uploading the original PNG with its metadata."}
    mask = (1 << 64) - 1
    distance = lambda row: bin((int(row["hash"]) ^ image_hash) & mask).count("1")
    best_distance = min(map(distance, rows))
    closest = [row for row in rows if distance(row) == best_distance]
    fields = []
    for row in closest:
        if row["model"] in EDIT_MODELS or row.get("edit_provenance"):
            fields.append(edit_metadata(row, _object(row.get("edit_provenance"))))
        else:
            fields.append({label: row.get(key) for label, key in (
                ("Prompt", "prompt"), ("Negative Prompt", "negative_prompt"), ("Seed", "seed"),
                ("Cfg", "cfg"), ("Model", "model"), ("loras", "loras"))})
    if len(rows) >= 101 or any(item != fields[0] for item in fields[1:]):
        return {"Result": "Several images match this image's visual hash, so I can't identify its prompt reliably. Please upload the original PNG with its metadata."}
    metadata = fields[0]
    metadata["CreateDate"] = closest[0].get("created_date", "N/A")
    metadata["Lookup"] = "Matched by visual hash; visually similar images can share a hash."
    return metadata


def metadata_embeds(metadata, image_url):
    """Keep long edit instructions within Discord's per-message embed limits."""
    def new_embed():
        return discord.Embed(title="Image Metadata", description=f"Metadata for {image_url}"[:4096], color=0x00FF00)

    embeds = []
    embed = new_embed()
    for key, original_value in metadata.items():
        value = str(original_value) if original_value is not None else "N/A"
        value = value or "N/A"
        name = str(key)[:256]
        for start in range(0, len(value), 1000):
            chunk = value[start:start + 1000]
            field_name = name if start == 0 else f"{name[:240]} (continued)"
            if len(embed.fields) >= 25 or len(embed) + len(field_name) + len(chunk) > 5900:
                embeds.append(embed)
                embed = new_embed()
            embed.add_field(name=field_name, value=chunk, inline=False)
    if embed.fields:
        embeds.append(embed)
    return embeds
