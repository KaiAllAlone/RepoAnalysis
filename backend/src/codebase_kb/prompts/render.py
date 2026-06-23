# backend/src/codebase_kb/prompts/render.py
from __future__ import annotations
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

# Locate the directory where this file (render.py) lives
_TPL_DIR = Path(__file__).parent

# Configure the Jinja environment
_env = Environment(
    loader=FileSystemLoader(str(_TPL_DIR)),
    # Disable autoescaping for Markdown files so Jinja doesn't mess up code blocks
    autoescape=select_autoescape(disabled_extensions=("md",), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

def render_prompt(name: str, **vars) -> str:
    """Load `name.md` and render with `vars`."""
    return _env.get_template(f"{name}.md").render(**vars) #