import os
import json
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timedelta
import random
import string
import asyncio

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

async def grab_image_hashes_to_convert():
    print("Grabbing image hashes to convert")

    select_query = """
        SELECT * FROM ImageHashes ORDER BY created_date DESC LIMIT 10000000;
    """

    async with await psycopg.AsyncConnection.connect(DSN) as aconn:
        async with aconn.cursor() as acur:
            await acur.execute(select_query)
            records = await acur.fetchall()

    return records


async def insert_image_hashes(image_hash, prompt, negative, seed, cfg, model, created_date):
    print("Inserting image hashes")

    insert_query = """
        INSERT INTO hashes (hash, prompt, negative_prompt, seed, cfg, model, created_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    values = [
        (
            image_hash,
            prompt,
            negative,
            seed,
            cfg,
            model,
            created_date,
        )
    ]

    async with await psycopg.AsyncConnection.connect(DSN) as aconn:
        async with aconn.cursor() as acur:
            # Use executemany to insert multiple records
            await acur.executemany(insert_query, values)
            await aconn.commit()  # Commit the transaction

def twos_complement(hexstr, bits):
    value = int(hexstr, 16)  # convert hexadecimal to integer

    # convert from unsigned number to signed number with "bits" bits
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value

async def main():
    records = await grab_image_hashes_to_convert()
    
    # Split records into chunks for concurrent processing
    def chunks(lst, n):
        """Yield successive n-sized chunks from lst."""
        for i in range(0, len(lst), n):
            yield lst[i:i + n]
    
    chunk_size = len(records) // 20  # Determine chunk size for 20 concurrent tasks
    records_chunks = list(chunks(records, chunk_size))

    async def process_chunk(chunk):
        for record in chunk:
            image_hash = twos_complement(str(record[0]), 64)
            prompt = record[1]
            negative = record[2]
            seed = record[3]
            cfg = record[4]
            model = record[5]
            created_date = record[6]
            await insert_image_hashes(image_hash, prompt, negative, seed, cfg, model, created_date)
    
    # Create a task for each chunk
    tasks = [asyncio.create_task(process_chunk(chunk)) for chunk in records_chunks]
    await asyncio.gather(*tasks)  # Run the tasks concurrently

if __name__ == "__main__":
    asyncio.run(main())