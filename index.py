import os
import json
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timedelta
import random
import string

import discord
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

    async def on_message(self, message):
        if message.author == self.user:
            return

        if message.content.lower().startswith('!getinfo'):
            args = message.content.split(" ")
            if len(args) < 2:
                await message.channel.send('Please provide an image URL')
                return

            imageUrl = args[1]
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
                metaData += f"model: {'Unable to check metadata for requested image, verify it exist.'}\n"
                
            # Send the data as a message
            if (len(metaData) >= 1800):
                await message.channel.send(f"Metadata for {imageUrl}:\n{metaData[:1800]}")
                await message.channel.send(f"{metaData[1800:]}")
            else:
                await message.channel.send(f"Metadata for {imageUrl}:\n{metaData}")

        elif message.content.lower().startswith('!fastpass'):
            args = message.content.split(" ")
            if len(args) < 3:  # Check if a duration and a user are specified
                await message.channel.send('Please specify the duration for the fastpass and mention a user, e.g. !fastpass 1week @username')
                return

            duration = args[1].lower().strip()
            if duration == '1week':
                expiration_date = datetime.now() + timedelta(weeks=1)
            elif duration == '1day':
                expiration_date = datetime.now() + timedelta(days=1)
            else:
                await message.channel.send(f'Unsupported duration: {duration}')
                return

            # Generate a new fastpass code
            fastpass_code = generate_fastpass_code()

            # Store the new fastpass code in a database or shared location
            store_fastpass_code(fastpass_code, datetime.now(), expiration_date, str(message.author))

            # Check if a user was mentioned
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
                except discord.Forbidden:
                    await message.channel.send(f"I don't have permission to DM {user.name}.")
                except discord.HTTPException as e:
                    await message.channel.send(f'An error occurred while trying to DM {user.name}: {e}')
            else:
                # If no user was mentioned, send the message in the channel as usual
                await message.channel.send(f'Your new fastpass code is {fastpass_code}')

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
