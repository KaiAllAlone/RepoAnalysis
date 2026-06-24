import asyncio
import base64
from typing import List, Optional, Tuple
import httpx
import dotenv
import os
from dotenv import load_dotenv
from src.codebase_kb.crawler.github import get_output

load_dotenv()

client_secret=os.getenv("client_secret")
client_id=os.getenv("client_id")


if __name__== "__main__":
    repository_url="https://github.com/KaiAllAlone/Q-Learning-From-Scratch"
    entries=asyncio.run(get_output(repository_url,client_id))
    for path,content in entries.items():
        print("*"*50,"\n")
        print(path)
        print("*"*50,"\n")
        print(content)
        print("*"*50,"\n")


    

