import asyncio
import base64
from typing import List, Optional, Tuple
import httpx
from src.codebase_kb.crawler.models.models import FileEntry,CommitEntry
from src.codebase_kb.crawler.utils.tree_parser import build_tree,print_tree
import dotenv
import tqdm
import os
from typing import Dict,List
from dotenv import load_dotenv
import requests

load_dotenv()

class GitHubRateLimitExceeded(Exception):
    """raised when GitHub API rate limit is exceeded."""
    pass


client_secret=os.getenv("client_secret")
client_id=os.getenv("client_id")
project_tree=None
output=None
async def exchange_code_for_token(
    client_id: str,
) -> str:
    """
    Exchanges the temporary GitHub OAuth code for a permanent access token.
    """
    headers = {
        "Accept": "application/json"  # Tells GitHub to return JSON instead of URL-encoded text
    }
    async with httpx.AsyncClient() as client:
        url = "https://github.com/login/device/code"
        
        data = {
            "client_id": client_id,
        }
        
        device_code_response=await asyncio.to_thread(requests.post,url=url,data=data,headers=headers)
        device_code_response=device_code_response.json()
        print('='*50)
        print("\n")
        print("User Code= ",device_code_response['user_code'],"\n")
        print("Device Code= ",device_code_response['device_code'],"\n")
        print("verification URI= ",device_code_response['verification_uri'],"\n")
        print("Please go to the Verfication URI  to authorize")
        print("\n")
        interval=device_code_response['interval']
        device_code=device_code_response['device_code']
        print('='*50)
        token_url = "https://github.com/login/oauth/access_token"
        poll_data = {
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
            }
            
        while True:
            await asyncio.sleep(interval) # Wait the required minimum timeframe
            poll_response = await client.post(token_url, data=poll_data, headers=headers)
            poll_result = poll_response.json()
            
            # If we get the token, return it!
            if "access_token" in poll_result:
                print("\nSuccess! Token acquired.")
                return poll_result["access_token"]
                
            # If still pending, just keep looping
            error = poll_result.get("error")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval += 5 # GitHub says to wait 5 seconds longer
            else:
                raise Exception(f"OAuth Error: {poll_result}")

async def _github_request(
client: httpx.AsyncClient,
method: str,
url: str,
token: str,
**kwargs
) -> httpx.Response:
    """Make a request to GitHub API with authentication and rate limit handling."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        **kwargs.pop("headers", {}),
    }
    if token and token.strip():
        headers["Authorization"] = f"token {token}"
    response = await client.request(method, url, headers=headers, **kwargs)

    # Check for rate limit exceeded
    if response.status_code in (403, 429):
        remaining = response.headers.get("X-RateLimit-Remaining")

        if remaining == "0":
            raise GitHubRateLimitExceeded(
                f"Rate limit exceeded. Reset at "
                f"{response.headers.get('X-RateLimit-Reset')}"
            )
    # For other errors, raise for status
    response.raise_for_status()
    return response

async def fetch_commit_history(
    repo_url: str,
    github_token: str,
    branch: Optional[str] = None,
    limit: int = 100  # Default to last 100 commits to avoid massive downloads
) -> List[CommitEntry]:
    """
    Fetch the commit history for a specific repository.
    """
    owner, repo = _parse_github_url(repo_url)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        if branch is None:
            branch = await _get_default_branch(client, owner, repo, github_token)
        commits_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        fetched_commits: List[CommitEntry] = []
        page = 1
        with tqdm.tqdm(total=limit, desc=f"Fetching Commits for {repo}", unit="commit") as pbar:
            while len(fetched_commits) < limit:
                # GitHub allows max 100 per page
                per_page = min(100, limit - len(fetched_commits))
                params = {
                    "sha": branch,
                    "per_page": per_page,
                    "page": page
                }
                try:
                    response = await _github_request(client, "GET", commits_url, github_token, params=params)
                    data = response.json()
                    # if data is empty, then reached the very earliest commit of the repo
                    if not data:
                        break
                    for item in data:
                        # extract the valuable parts of the commit payload
                        fetched_commits.append(CommitEntry(
                            sha=item["sha"],
                            author=item["commit"]["author"]["name"],
                            date=item["commit"]["author"]["date"],
                            message=item["commit"]["message"]
                        ))
                        pbar.update(1)
                        
                        if len(fetched_commits) >= limit:
                            break
                    page += 1
                except httpx.HTTPStatusError as e:
                    tqdm.tqdm.write(f"Failed to fetch commit page {page}: {e}")
                    break

        return fetched_commits

async def fetch_github_repo(
    repo_url: str,
    github_token: str,
    branch: Optional[str] = None,
    max_file_size: int = 100 * 1024,  # 100 KB
) -> List[FileEntry]:
    """
    Fetch a GitHub repository and return a list of FileEntry objects.

    Args:
        repo_url: GitHub repository URL (https://github.com/owner/repo or git@github.com:owner/repo.git)
        github_token: User's OAuth token (decrypted)
        branch: Branch name (if None, uses default branch)
        max_file_size: Maximum file size in bytes to include

    Returns:
        List of FileEntry objects with path and content.

    Raises:
        GitHubRateLimitExceeded: If GitHub API rate limit is exceeded.
        httpx.HTTPStatusError: For other HTTP errors.
    """
    # Parse owner and repo from URL
    owner, repo = _parse_github_url(repo_url)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Determine branch
        if branch is None:
            branch = await _get_default_branch(client, owner, repo, github_token)

        # Get commit SHA for the branch
        commit_sha = await _get_branch_commit(client, owner, repo, branch, github_token)

        # Get recursive tree
        tree = await _get_tree_recursive(client, owner, repo, commit_sha, github_token)

        # Process each file in the tree
        entries: List[FileEntry] = []
        files_only = [item for item in tree if item["type"] == "blob" and (item["path"].endswith(".py"))]
        for item in tqdm.tqdm(files_only, desc=f"Downloading {owner}/{repo}", unit="file"):
            path = item["path"]
            # Get raw bytes of the file
            raw_bytes = await _get_file_raw_bytes(
                client, owner, repo, path, branch, github_token
            )
            # Skip if too large
            if len(raw_bytes) > max_file_size:
                continue
            # Check for binary (null byte in first 8KB)
            if _is_binary(raw_bytes):
                continue
            # Decode to string
            try:
                content = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                #skip as binary
                continue
            entries.append(FileEntry(path=path, content=content))
        return entries

def _parse_github_url(repo_url: str) -> Tuple[str, str]:
    """Extract owner and repo from GitHub URL."""
    # Remove protocol and git@
    if repo_url.startswith("https://github.com/"):
        repo_url = repo_url[len("https://github.com/"):]
    elif repo_url.startswith("git@github.com:"):
        repo_url = repo_url[len("git@github.com:"):]
    else:
        raise ValueError(f"Unsupported GitHub URL format: {repo_url}")
    if repo_url.endswith(".git"):
        repo_url = repo_url[:-4]
    parts = repo_url.split("/")
    if len(parts) < 2:
        raise ValueError(f"Could not parse owner/repo from URL: {repo_url}")
    owner = parts[0]
    repo = parts[1]
    return owner, repo

async def _get_default_branch(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    token: str
) -> str:
    """Get the default branch for a repository."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    response = await _github_request(client, "GET", url, token)
    data = response.json()
    return data["default_branch"]

async def _get_branch_commit(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
    token: str
) -> str:
    """Get the commit SHA for a branch."""
    """ SHA represents the commit uniquely through a stream of chars"""
    url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}"
    response = await _github_request(client, "GET", url, token)
    data = response.json()
    return data["commit"]["sha"]

async def _get_tree_recursive(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    commit_sha: str,
    token: str
) -> List[dict]:
    """Get recursive tree of the repository."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{commit_sha}"
    params = {"recursive": "1"}
    response = await _github_request(client, "GET", url, token, params=params)
    data = response.json()
    paths=[]
    for d in data["tree"]:
        paths.append(d["path"])
    # project_tree is built but not stored globally
    _ = build_tree(paths=paths)
    return data["tree"]

async def _get_file_raw_bytes(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    path: str,
    branch: str,
    token: str
) -> bytes:
    """Get the raw bytes of a single file via Contents API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": branch}
    response = await _github_request(client, "GET", url, token, params=params)
    data = response.json()
    # Content is base64 encoded
    raw_bytes = base64.b64decode(data["content"])
    return raw_bytes

def _is_binary(content: bytes) -> bool:
    """
    Check if content is binary by looking for null byte in first 8KB.
    """
    # Check first 8192 bytes for null byte
    return b"\x00" in content[:8192]

async def get_output(
    repo_url: str,
    client_id: str | None = None,
):
    github_token = os.getenv("GITHUB_TOKEN", "")

    try:
        return await fetch_github_repo(
            repo_url=repo_url,
            github_token=github_token,
        )

    except GitHubRateLimitExceeded:

        print("\nAnonymous GitHub API quota exhausted.")

        if not client_id:
            client_id = await asyncio.to_thread(
                input,
                "Enter GitHub OAuth App Client ID: "
            )
            client_id = client_id.strip()

        while True:
            try:
                github_token = await exchange_code_for_token(client_id)
                break

            except Exception as e:
                print(f"OAuth failed: {e}")

                client_id = await asyncio.to_thread(
                    input,
                    "Enter a valid GitHub OAuth App Client ID: "
                )
                client_id = client_id.strip()

        dotenv.set_key(".env", "GITHUB_TOKEN", github_token)
        load_dotenv(override=True)
        print("\nRetrying with authenticated GitHub API...\n")

        return await fetch_github_repo(
            repo_url=repo_url,
            github_token=github_token,
        )