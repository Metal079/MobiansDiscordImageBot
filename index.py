import os
import json
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timedelta
import random
import string
import asyncio

import discord
from discord import Embed
from discord.ext import commands
from discord import Intents
from PIL import Image
import requests

from dotenv import load_dotenv
import pyodbc

DBHOST = os.environ.get('DBHOST')
DBNAME = os.environ.get('DBNAME')
DBUSER = os.environ.get('DBUSER')
DBPASS = os.environ.get('DBPASS')
driver= '{ODBC Driver 17 for SQL Server}'
cnxn = pyodbc.connect('DRIVER='+driver+';SERVER='+DBHOST+';PORT=1433;DATABASE='+DBNAME+';UID='+DBUSER+';PWD='+ DBPASS)

class MyBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, intents=Intents.all())

    async def on_ready(self):
        print(f'We have logged in as {self.user}')

    def get_random_image_path(self, username):
        images_folder = 'sfw'
        cursor = cnxn.cursor()

        # Get the images that the user has already tagged
        cursor.execute("SELECT ImagePath FROM [Mobians].[dbo].[Captions] WHERE UserName = ?", username)
        tagged_images = [row[0] for row in cursor.fetchall()]

        # Get all images from the folder
        all_images = [os.path.join(images_folder, f) for f in os.listdir(images_folder) if os.path.isfile(os.path.join(images_folder, f))]

        # Exclude images that the user has already tagged
        untagged_images = [image for image in all_images if image not in tagged_images]

        return random.choice(untagged_images) if untagged_images else None

    def update_image_tag(self, image_path, tag, username):
        cursor = cnxn.cursor()
        # Insert the caption, image path, and created date into the Captions table
        cursor.execute("""
            INSERT INTO [Mobians].[dbo].[Captions] (UserName, Caption, ImagePath, CreatedDate)
            VALUES (?, ?, ?, GETDATE())
        """, username, tag, image_path)

        cnxn.commit()

    async def on_message(self, message):
        if message.author == self.user:
            return

        if message.content.lower().startswith('!getinfo'):
            imageUrl = ""
            # Case 1: URL immediately after the command
            if len(message.content) > len('!getinfo'):
                imageUrl = message.content[len('!getinfo'):].strip()
            else:
                # Case 2: URL in a new line
                args = message.content.split("\n")
                if len(args) > 1:
                    imageUrl = args[1].strip()
            
            if not imageUrl:
                await message.channel.send('Please provide an image URL')
                return

            imageUrl = trim_url_to_extension(imageUrl)
            
            # Retrieve the image data
            response = requests.get(imageUrl)
            image = Image.open(BytesIO(response.content))
            
            # Get the metadata
            info = image.info
            metaData = f""

            if 'Disclaimer' in info: # Detect if the image is from mobians.ai
                if 'prompt' in info:
                    metaData += f"Prompt: {info['prompt']}\n"
                if 'negative_prompt' in info:
                    # Remove 'admin' from the negative_prompt
                    negative_prompt = info['negative_prompt'].replace(', admin', '')
                    metaData += f"Negative Prompt: {negative_prompt}\n"
                if 'seed' in info:
                    metaData += f"Seed: {info['seed']}\n"
                if 'cfg' in info:
                    metaData += f"Cfg: {info['cfg']}\n"
                metaData += f"model: {'mobians.ai / SonicDiffusionV3Beta4'}\n"
            elif 'parameters' in info: # Detect if the image is from auto1111
                # Split the parameters string into individual metadata items
                params = info['parameters'].split('\n')
                for param in params:
                    # Split each item into a key and value
                    try:
                        key, value = param.split(': ')
                    except ValueError:
                        key, value = 'prompt: ', param
                    metaData += f"{key}: {value}\n"
            elif 'invokeai' in info:
                data = json.loads(info['invokeai'])
                for key, value in data.items():
                    metaData += f"{key}: {value}\n"
            elif 'prompt' in info:
                metaData += f"Prompt: {info['prompt']}\n"
                metaData += f"Negative Prompt: {info['negative_prompt']}\n"
                metaData += f"Seed: {info['seed']}\n"
                metaData += f"Cfg: {info['guidance_scale']}\n"
                metaData += f"model: {info['use_stable_diffusion_model'].split('stable-diffusion')[-1]}\n"
            else:
                metaData += f"model: {'Unable to check the metadata for the requested image. It may not have prompts embedded'}\n"
                
            # Send the data as a message
            if (len(metaData) >= 1800):
                await message.channel.send(f"Metadata for {imageUrl}:\n{metaData[:1800]}")
                await message.channel.send(f"{metaData[1800:]}")
            else:
                await message.channel.send(f"Metadata for {imageUrl}:\n{metaData}")

        elif message.content.lower().startswith('!fastpass'):
            # Check if the author has the "Moderator" role
            if any(role.name == 'Mod' for role in message.author.roles):
                args = message.content.split(" ")
                if len(args) < 3:  # Check if a duration and a user are specified
                    await message.channel.send('Please specify the duration for the fastpass and mention a user, e.g. !fastpass 1week @username')
                    return

                duration = args[1].lower().strip()
                if 'week' in duration:
                    # Get timedelta from numbers immediately before the word 'week'
                    try:
                        duration = int(duration.replace('week', '').strip())
                    except ValueError:
                        duration = int(duration.replace('weeks', '').strip())
                    expiration_date = datetime.now() + timedelta(weeks=duration)
                elif 'day' in duration:
                    try:
                        duration = int(duration.replace('day', '').strip())
                    except ValueError:
                        duration = int(duration.replace('days', '').strip())
                    expiration_date = datetime.now() + timedelta(days=duration)
                elif 'month' in duration:
                    try:
                        duration = int(duration.replace('month', '').strip())
                    except ValueError:
                        duration = int(duration.replace('months', '').strip())
                    expiration_date = datetime.now() + timedelta(months=duration)
                else:
                    await message.channel.send(f'Unsupported duration: {duration}')
                    return

                # Generate a new fastpass code
                fastpass_code = generate_fastpass_code()

                # Store the new fastpass code in a database or shared location
                store_fastpass_code(fastpass_code, datetime.now(), expiration_date, str(message.author))

                # Check if a user was mentioned
                if len(message.mentions) > 0:
                    # Send a DM to the first mentioned user
                    user = message.mentions[0]
                    try:
                        await user.send(
                            f'Your new fastpass code is {fastpass_code}.\n'
                            f'Your pass will expire on {expiration_date}.\n'
                            f'Thank you for being an active member of the community!\n'
                            f'We hope to see you again in future server events!.'
                        )
                        await message.channel.send(f'Fastpass code has been sent to {user.name}')
                    except discord.Forbidden:
                        await message.channel.send(f"I don't have permission to DM {user.name}.")
                    except discord.HTTPException as e:
                        await message.channel.send(f'An error occurred while trying to DM {user.name}: {e}')
                else:
                    # If no user was mentioned, send the message in the channel as usual
                    await message.channel.send(f'Your new fastpass code is {fastpass_code}')
            else:
                await message.channel.send('You do not have permission to use this command.')

        elif message.content.lower().strip() == '!caption' or message.content.lower().strip() == '!tag':
            caller_username = message.author.name
            image_path = self.get_random_image_path(caller_username)

            if image_path is None:
                await message.channel.send(f"You've captioned all available images, {message.author.mention}!")
                return

            with open(image_path, 'rb') as img_file:
                img_data = img_file.read()
            image_name = os.path.basename(image_path)
            file = discord.File(BytesIO(img_data), filename=image_name)

            embed = Embed(title=f"Reply to this message to caption it, {message.author.mention}!")
            embed.set_image(url=f"attachment://{image_name}")
            image_message = await message.channel.send(embed=embed, file=file)

            cursor = cnxn.cursor()  # Define the cursor

            def check(reply):
                # Check if the reply is to the specific message containing the image
                return reply.reference and reply.reference.message_id == image_message.id

            # Wait for the user's reply for tagging without a timeout
            reply = await self.wait_for('message', check=check)

            # Get the username of the person who is replying
            username = reply.author.name

            cursor.execute("SELECT COUNT(*) FROM [Mobians].[dbo].[Captions] WHERE UserName = ? AND ImagePath = ?", username, image_path)
            already_tagged = cursor.fetchone()[0]
            if already_tagged:
                await message.channel.send(f"You've already captioned this image, {reply.author.mention}!")
                return

            tag = reply.content.strip()

            # Validate the tag length
            if len(tag) <= 380:
                # Update the database entry for the image with the tag
                self.update_image_tag(image_path, tag, username)

                # Send the confirmation message
                await message.channel.send(f"Image caption confirmed, Thank you! {reply.author.mention}!")
            else:
                await message.channel.send(f"The caption is too long (max 380 characters). Please try again {reply.author.mention}!")


        elif message.content.lower().strip() == '!rank':
            username = message.author.name
            cursor = cnxn.cursor()

            # Execute the query to get the user's rank and number of images tagged
            cursor.execute("""
                WITH RankedUsers AS (
                    SELECT UserName, ImagesCaptioned, RANK() OVER (ORDER BY ImagesCaptioned DESC) AS Rank
                    FROM [dbo].[Vw_UserCaptions]
                )
                SELECT UserName, ImagesCaptioned, Rank
                FROM RankedUsers
                WHERE UserName = ?
            """, username)
            
            result = cursor.fetchone()
            if result:
                images_captioned = result.ImagesCaptioned
                rank = result.Rank
                await message.channel.send(f"{message.author.mention}, you have tagged {images_captioned} images and are ranked #{rank}!")
            else:
                await message.channel.send(f"{message.author.mention}, you have not tagged any images yet.")


def trim_url_to_extension(url):
    parsed_url = urlparse(url)
    file_name_with_extension = parsed_url.path.split('/')[-1]
    file_name, extension = file_name_with_extension.rsplit('.', 1)
    trimmed_url = url[:url.index(extension)+len(extension)]
    return trimmed_url

def generate_fastpass_code():
    words = []
    for _ in range(4):  # Generate four words
        word_length = random.randint(2, 3)  # Each word has 2-3 characters
        word = ''.join(random.choice(string.ascii_lowercase) for _ in range(word_length))  # Randomly choose characters
        words.append(word)
    return '-'.join(words)  # Combine words with '-'

def store_fastpass_code(fastpass_code, creation_date, expiration_date, created_by):
    global cnxn  # Declare cnxn as a global variable
    cursor = cnxn.cursor()

    cursor.execute("""
        INSERT INTO FastPass 
        (FastPassCode, CreationDate, ExpirationDate, CreatedBy) 
        VALUES (?, ?, ?, ?)
    """, fastpass_code, creation_date, expiration_date, created_by)

    cnxn.commit()


bot = MyBot()

load_dotenv()
token = os.environ.get('token')
bot.run(token)
