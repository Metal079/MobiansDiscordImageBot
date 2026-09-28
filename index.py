import asyncio
import contextlib
from generation_monitor import run_bot_monitor
import os
import json
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timedelta
import random
import string

import discord
from discord.ext import commands
from discord import app_commands, Attachment
from PIL import Image
import requests
import imagehash

# from dotenv import load_dotenv
import psycopg
import aiohttp
from aiohttp import web

DBHOST = os.environ.get("DBHOST")
DBNAME = os.environ.get("DBNAME")
DBUSER = os.environ.get("DBUSER")
DBPASS = os.environ.get("DBPASS")

DSN = f"host={DBHOST} dbname='{DBNAME}' user={DBUSER} password={DBPASS}"


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # Enable guild member intents
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session = None  # Initialize session attribute
        self.generation_monitor_task = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            trust_env=True
        )  # Initialize the session here
        await self.tree.sync(guild=discord.Object(id=1095514548112461924))
        print(f"Successfully slash commands for {self.user} with Discord!")
        # Mobians generation availability monitoring
        self.generation_monitor_task = asyncio.create_task(run_bot_monitor(self, DSN), name="generation-availability-monitor")

    async def on_ready(self):
        print(f"We have logged in as {self.user}")

        # Start the web server
        await self.start_server()
        print("Started web server")

    async def start_server(self):
        app = web.Application()
        app.router.add_post("/check_role", self.handle_role_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 6965)  # Choose your host and port
        await site.start()

    async def handle_role_check(self, request):
        data = await request.json()
        guild_id = data["guild_id"]
        user_id = data["user_id"]
        role_ids = data["role_ids"]  # Expect a list of role IDs

        for role_id in role_ids:
            if await self.has_role(guild_id, user_id, role_id):
                return web.json_response({"has_role": True})

        return web.json_response({"has_role": False})

    async def has_role(self, guild_id, user_id, role_id):
        guild = await self.fetch_guild(guild_id)
        if guild:
            member = await guild.fetch_member(user_id)
            if member:
                return int(role_id) in [role.id for role in member.roles]
        return False

    async def close(self):
        if self.generation_monitor_task is not None:
            self.generation_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.generation_monitor_task
        await self.session.close()  # Close the session on bot shutdown
        await super().close()


def trim_url_to_extension(url):
    parsed_url = urlparse(url)
    file_name_with_extension = parsed_url.path.split("/")[-1]

    # Check if there's a dot in the file name, if not return the original URL
    if "." not in file_name_with_extension:
        return url

    file_name, extension = file_name_with_extension.rsplit(".", 1)

    # Case insensitive check for extension in URL
    extension_start = url.lower().rfind(extension.lower())

    # If the extension is not found, return the original URL
    if extension_start == -1:
        return url

    trimmed_url = url[: extension_start + len(extension)]
    return trimmed_url


def generate_fastpass_code():
    code_parts = []
    for _ in range(4):  # Generate four parts
        part_length = random.randint(2, 3)  # Each part has 2-3 characters/digits
        # Use both lowercase letters and digits
        characters = string.ascii_lowercase + string.digits
        part = ''.join(random.choice(characters) for _ in range(part_length))
        code_parts.append(part)
    return "-".join(code_parts) 


async def store_fastpass_code(
    fastpass_code, creation_date, created_by, fastpass_days, is_admin, user=None, nickname=None
):
    async with await psycopg.AsyncConnection.connect(DSN) as aconn:
        async with aconn.cursor() as acur:
            await acur.execute(
                """
                INSERT INTO fastpass_new 
                (fastpass_code, creation_date, created_by, fastpass_days, is_admin, assigned_to, assigned_to_nickname) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    fastpass_code,
                    creation_date,
                    created_by,
                    fastpass_days,
                    is_admin,
                    user,
                    nickname,
                ),
            )

            await aconn.commit()


def _is_mod_or_admin(member: discord.abc.User) -> bool:
    roles = getattr(member, "roles", [])
    if any(getattr(role, "name", None) == "Mod" for role in roles):
        return True
    permissions = getattr(getattr(member, "guild_permissions", None), "administrator", False)
    return bool(permissions)


async def add_credits_for_discord_user(
    discord_user_id: str,
    amount: int,
    *,
    transaction_type: str = "admin_adjustment",
    description: str = "POTW contest award",
    created_by: str | None = None,
):
    if amount <= 0:
        raise ValueError("amount must be positive")

    async with await psycopg.AsyncConnection.connect(DSN) as aconn:
        async with aconn.cursor() as acur:
            await acur.execute(
                """
                UPDATE public.users
                SET credits = credits + %s
                WHERE discord_user_id = %s
                RETURNING id, credits
                """,
                (amount, discord_user_id),
            )
            row = await acur.fetchone()
            if not row:
                return {"found": False, "user_id": None, "new_balance": None}

            user_id, new_balance = row

            full_description = description
            if created_by:
                full_description = f"{description} (granted by {created_by})"

            await acur.execute(
                """
                INSERT INTO public.credit_transactions
                    (user_id, amount, balance_after, transaction_type, description)
                VALUES
                    (%s, %s, %s, %s, %s)
                """,
                (user_id, amount, new_balance, transaction_type, full_description),
            )
            await aconn.commit()

            return {"found": True, "user_id": user_id, "new_balance": new_balance}


client = MyBot()


@client.hybrid_command(name="getinfo", description="Get the metadata of an image")
@app_commands.guilds(discord.Object(id=1095514548112461924))
@app_commands.describe(url="Get the metadata of an image")
async def get_info(ctx, url: str = None):
    image_url = None

    # Check if the command is invoked via slash command and handle accordingly
    if isinstance(ctx, discord.Interaction):
        # Slash command logic; URL must be provided as there's no attachment support directly
        if url is None:
            await ctx.response.send_message(
                "Please provide an image URL.", ephemeral=False
            )
            return
        await ctx.response.defer()
        image_url = url

    # For traditional command invocation, check for attachments in the message
    else:
        if ctx.message.attachments:
            # If there's an attachment, use the first one
            attachment = ctx.message.attachments[0]
            image_url = attachment.url
        elif url:
            # If a URL is provided in the command, use it
            image_url = url
        else:
            # If neither an attachment nor a URL is provided, send an error message
            await ctx.send("Please provide an image URL or an attachment.")
            return

    # Proceed to fetch and process the image using `image_url`
    response = requests.get(image_url)
    if response.status_code == 200:
        image = Image.open(BytesIO(response.content))
    else:
        await ctx.send(
            f"Failed to fetch the image. Status code: {response.status_code}"
        )

    # Get the metadata
    info = image.info
    metadata_dict = {}

    # Depending on the metadata source, you may adjust the way you handle each case
    if "Disclaimer" in info:  # Detect if the image is from mobians.ai
        metadata_dict["Prompt"] = info.get("prompt", "N/A")
        metadata_dict["Negative Prompt"] = info.get("negative_prompt", "N/A")
        metadata_dict["Seed"] = info.get("seed", "N/A")
        metadata_dict["Cfg"] = info.get("cfg", "N/A")
        metadata_dict["Model"] = info.get('model')
        metadata_dict["loras"] = info.get("loras", " ")
    elif "request_id" in info:  # Detect if the image is from NeverSFW.gg
        metadata_dict["Prompt"] = info.get("prompt", "N/A")
        metadata_dict["Loras"] = info.get("loras", "N/A")
        metadata_dict["Cfg"] = info.get("CFG", "N/A")
        metadata_dict["Steps"] = info.get("steps", "N/A")
        metadata_dict["Generation Type"] = info.get("model_type", "N/A")
        metadata_dict["Generation Date"] = info.get("generation_date", "N/A")
    elif "parameters-json" in info:  # Detect if the image is from comfy?
        data = json.loads(info["parameters-json"])
        metadata_dict["Prompt"] = data.get("PositivePrompt", "N/A")
        metadata_dict["Negative Prompt"] = data.get("NegativePrompt", "N/A")
        metadata_dict["Seed"] = data.get("Seed", "N/A")
        metadata_dict["Cfg"] = data.get("CfgScale", "N/A")
        metadata_dict["Model"] = data.get("ModelName", "N/A")
    elif "parameters" in info:  # Detect if the image is from auto1111
        params = info["parameters"].split("\n")
        metadata_dict["Prompt"] = params[0]
        metadata_dict["Negative Prompt"] = params[1]
        try:
            key, value = params[2].split(", ")
        except:
            try:
                key, value = "Misc Info", params[2]
            except IndexError:
                key, value = "Misc Info", params[1]
                # delete negative prompt metadata
                metadata_dict.pop("Negative Prompt")
                
        metadata_dict[key] = value
    elif "invokeai" in info:
        data = json.loads(info["invokeai"])
        for key, value in data.items():
            metadata_dict[key] = value
    elif "prompt" in info:  # Handling for different metadata structure
        metadata_dict["Prompt"] = info.get("prompt", "N/A")
        metadata_dict["Negative Prompt"] = info.get("negative_prompt", "N/A")
        metadata_dict["Seed"] = info.get("seed", "N/A")
        metadata_dict["Cfg"] = info.get("guidance_scale", "N/A")
        metadata_dict["Model"] = info.get(
            "use_stable_diffusion_model", "Unknown model"
        ).split("stable-diffusion")[-1]
    else:
        metadata_dict["Model"] = (
            "Unable to check the metadata for the requested image. It may not have prompts embedded. Please use https://exif.tools/ to confirm. Ask the Author for prompts if available"
        )
        metadata_dict["hash_notice"] = "Attempting to find image based on hash"
        image_hash = str(imagehash.phash(image, hash_size=8))
        image_hash = twos_complement(image_hash, 64)

        print(f"Hash: {image_hash}")

        try:
            cutoff = 1  # maximum bits that could be different between the hashes.

            # Fetch details of the most similar image
            async with await psycopg.AsyncConnection.connect(DSN) as aconn:
                async with aconn.cursor() as acur:
                    await acur.execute(
                        "SELECT * FROM hashes WHERE hash <@ (%s, %s)",
                        (
                            image_hash,
                            cutoff,
                        ),
                    )
                    result = await acur.fetchone()
            if result:
                metadata_dict["Prompt"] = result[2]
                metadata_dict["Negative Prompt"] = result[3]
                metadata_dict["Seed"] = result[4]
                metadata_dict["Cfg"] = result[5]
                metadata_dict["Model"] = result[6]
                metadata_dict["CreateDate"] = result[7]
                metadata_dict['loras'] = result[8]
            else:
                metadata_dict["error"] = (
                    "Unable to find details for the most similar image based on hash"
                )
        except:
            pass

    # Now build the embed, send 2 if over 1024 characters
    embed = discord.Embed(
        title="Image Metadata",
        description=f"Metadata for {image_url}",
        color=0x00FF00,
    )
    for key, original_value in metadata_dict.items():
        try:
            value_length = len(original_value)
        except TypeError:
            value_length = len(
                str(original_value)
            )  # Convert to string if not a sequence

        if value_length > 1000:
            # Split after 1000 characters
            values = [
                original_value[i : i + 1000] for i in range(0, value_length, 1000)
            ]
            for i, val in enumerate(values):
                if i == 0:
                    embed.add_field(name=key, value=val, inline=False)
                else:
                    embed.add_field(name="\u200b", value=val, inline=False)
        else:
            embed.add_field(name=key, value=original_value, inline=False)

    # Send the embed
    try:
        await ctx.send(embed=embed)
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the embed: {e}")


@client.hybrid_command(name="fastpass", description="Grant a fastpass to a user")
@app_commands.guilds(discord.Object(id=1095514548112461924))
@app_commands.describe(duration="Duration of the fastpass (e.g., 1week, 2days, 1month)")
@app_commands.describe(duration="Grant a fastpass to a user")
async def fastpass(ctx, duration: str, user: str = None):
    # Check if the author has the "Moderator" role
    if any(role.name == "Mod" for role in ctx.author.roles):
        args = duration.split(" ")

        if user: # User is specified
            # Try to find the member
            # First remove any mentions from the user string
            user = user.replace("<", "").replace(">", "").replace("@", "").replace("!", "")
            member = ctx.guild.get_member_named(user)
            if member is None:
                try:
                    member = await ctx.guild.fetch_member(int(user))
                except (ValueError, discord.NotFound):
                    await ctx.send(f"User '{user}' not found. The fastpass will be generated without a specific user.")
                    member = None
        else:
            await ctx.send("Please mention a user, e.g. !fastpass 1week @username")
            return            

        # Extract days, weeks or months from the duration (ie, after the last digit)
        time_type = "".join(filter(str.isalpha, duration))


        if "day" in time_type:
            try:
                duration = int(duration.replace("day", "").strip())
            except ValueError:
                duration = int(duration.replace("days", "").strip())
        elif "week" in time_type:
            # Get timedelta from numbers immediately before the word 'week'
            try:
                duration = int(duration.replace("week", "").strip())
            except ValueError:
                duration = int(duration.replace("weeks", "").strip())
            finally:
                duration *= 7
        elif "month" in time_type:
            try:
                duration = int(duration.replace("month", "").strip())
            except ValueError:
                duration = int(duration.replace("months", "").strip())
            finally:
                duration *= 30
        else:
            await ctx.channel.send(f"Unsupported duration: {duration}")
            return

        # Generate a new fastpass code
        fastpass_code = generate_fastpass_code()

        # Store the new fastpass code in a database or shared location
        if member:
            await store_fastpass_code(
                fastpass_code,
                datetime.now(),
                str(ctx.author),
                fastpass_days=duration,
                is_admin=False,
                user=member.id,
                nickname=member.name,
            )
        else:
            await store_fastpass_code(
                fastpass_code,
                datetime.now(),
                str(ctx.author),
                fastpass_days=duration,
                is_admin=False,
            )

        # Generate the fastpass message
        fastpass_message = (
            f"New fastpass code: {fastpass_code}\n"
            f"This pass will expire {duration} day(s) after first use.\n"
            f"Thank you for being an active member of the community!\n"
            f"We hope to see you again in future server events!"
        )

        # When sending the message:
        if member:
            # Try to DM the member
            try:
                await member.send(fastpass_message)
                await ctx.send(f"Fastpass code has been sent to {member.name}")
            except discord.Forbidden:
                await ctx.send(f"Unable to DM {member.name}. Here's the fastpass information:")
                await ctx.send(fastpass_message)
            except discord.HTTPException as e:
                await ctx.send(f"An error occurred while trying to process for {member.name}: {e}")
                await ctx.send(fastpass_message)
        else:
            # If no user was mentioned or the user doesn't exist, send the message in the channel
            await ctx.send(fastpass_message)
    else:
        await ctx.send("You do not have permission to use this command.")


@fastpass.error
async def fastpass_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        if error.param.name == "duration":
            await ctx.send(
                "Error: Please include a duration for the fastpass (e.g., 1week, 2days, 1month)"
            )
    else:
        # Handle other types of errors
        await ctx.send(f"An error occurred: {str(error)}")


@client.hybrid_command(name="givecredits", description="Give POTW credits to a user")
@app_commands.guilds(discord.Object(id=1095514548112461924))
@app_commands.describe(member="User to grant credits to", amount="Credits to add (positive integer)")
async def givecredits(ctx, member: discord.Member, amount: int):
    if not _is_mod_or_admin(ctx.author):
        await ctx.send("You do not have permission to use this command.")
        return

    if amount <= 0:
        await ctx.send("Amount must be a positive integer.")
        return

    result = await add_credits_for_discord_user(
        str(member.id),
        amount,
        description="POTW contest award",
        transaction_type="promo",
        created_by=str(ctx.author),
    )

    if not result["found"]:
        await ctx.send(
            f"{member.mention} does not have an account on the website yet (no row in public.users for their Discord ID)."
        )
        return

    dm_message = (
        "Thank you for participating in our POTW contest!\n"
        f"Weâ€™ve added {amount} credits to your Mobians.ai account.\n"
        f"Your new balance is {result['new_balance']} credits."
    )

    dm_sent = False
    try:
        await member.send(dm_message)
        dm_sent = True
    except discord.Forbidden:
        dm_sent = False
    except discord.HTTPException:
        dm_sent = False

    if dm_sent:
        await ctx.send(
            f"Granted {amount} credits to {member.mention}. New balance: {result['new_balance']}. (DM sent)"
        )
    else:
        await ctx.send(
            f"Granted {amount} credits to {member.mention}. New balance: {result['new_balance']}. (Could not DM user)"
        )


@client.hybrid_command(name="givewinnercredits", description="Give 1500 POTW winner credits to a user")
@app_commands.guilds(discord.Object(id=1095514548112461924))
@app_commands.describe(member="Winner to grant credits to")
async def givewinnercredits(ctx, member: discord.Member):
    await givecredits(ctx, member, 1500)


@client.hybrid_command(name="giverunnerupcredits", description="Give 1000 POTW runner-up credits to a user")
@app_commands.guilds(discord.Object(id=1095514548112461924))
@app_commands.describe(member="Runner-up to grant credits to")
async def giverunnerupcredits(ctx, member: discord.Member):
    await givecredits(ctx, member, 1000)

@client.hybrid_command(name="giveparticipantcredits", description="Give 200 POTW participant credits to a user")
@app_commands.guilds(discord.Object(id=1095514548112461924))
@app_commands.describe(member="Participant to grant credits to")
async def giveparticipantcredits(ctx, member: discord.Member):
    await givecredits(ctx, member, 200)

def twos_complement(hexstr, bits):
    value = int(hexstr, 16)  # convert hexadecimal to integer

    # convert from unsigned number to signed number with "bits" bits
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


# load_dotenv()
token = os.environ.get("token")
client.run(token)