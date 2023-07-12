import os
import json
from io import BytesIO
from urllib.parse import urlparse

import discord
from discord import Intents
from PIL import Image
import requests

from dotenv import load_dotenv


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
            await message.channel.send(f"Metadata for {imageUrl}:\n{metaData}")

def trim_url_to_extension(url):
    parsed_url = urlparse(url)
    file_name_with_extension = parsed_url.path.split('/')[-1]
    file_name, extension = file_name_with_extension.rsplit('.', 1)
    trimmed_url = url[:url.index(extension)+len(extension)]
    return trimmed_url

bot = MyBot()

load_dotenv()
token = os.environ.get('token')
bot.run(token)
