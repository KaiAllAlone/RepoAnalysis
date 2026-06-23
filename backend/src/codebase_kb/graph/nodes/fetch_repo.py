import asyncio
from src.codebase_kb.crawler.github import fetch_github_repo

async def fetch_repo_node(state: dict) -> dict:
    """Node 1: downloads the code from github"""
    entries = await fetch_github_repo(
        repo_url=state["repo_url"],
        github_token=state["github_token"]
    )

    files_list = [{"path": e.path, "content": e.content} for e in entries]
    
    return {"files":files_list}