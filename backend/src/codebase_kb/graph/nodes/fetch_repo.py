import asyncio
from codebase_kb.crawler.github import fetch_github_repo, get_output
from src.codebase_kb.graph.state import KnowledgeBuilderState

async def fetch_repo_node(state: KnowledgeBuilderState) -> dict:
    """Node 1: downloads the code from github"""
    entries = await get_output(
        repo_url=state["repo_url"],
        # client_id=state["client_id"]
    )

    files_list = [{"path": e.path, "content": e.content} for e in entries]
    
    return {"files":files_list}