# Automated Codebase Knowledge Builder — Plan

## Context

Onboarding to a large, undocumented repository is one of the most expensive bottlenecks in software engineering. Developers are forced to manually trace imports and read files line-by-line to build a mental model of the architecture. Existing tools fail in different ways: wikis go stale, docstring generators are too granular, and semantic search (Bloop, Sourcegraph) requires the developer to already know what to look for.

**Goal**: build a Python CLI that ingests a Git repository (URL or local path), reverse-engineers its architecture, and emits a structured, human-readable Markdown tutorial (`index.md` + numbered chapter files) with auto-generated Mermaid diagrams. The narrative is LLM-synthesized; the diagrams are programmatic and guaranteed-accurate because they're built from validated LLM output, not emitted by the model.

**Reference architecture**: [PocketFlow-Tutorial-Codebase-Knowledge](https://the-pocket.github.io/PocketFlow-Tutorial-Codebase-Knowledge/) — a 6-node linear pipeline (Fetch → Identify → Analyze → Order → Write (BatchNode) → Combine) that we will re-implement in **LangGraph** (per user choice) with a pluggable LLM layer.

**User-confirmed choices**:
- Target language: **Python** (use `ast` stdlib for extraction)
- Orchestration: **LangGraph** StateGraph + `Send` for the chapter fan-out
- LLM providers: **Pluggable** — Gemini, Anthropic, Ollama, OpenAI-compatible
- Output: **Markdown** (`index.md` + `NN_name.md` + Mermaid `flowchart TD` / `sequenceDiagram`)

**Working directory**: `/home/debanuj/Desktop/Repo Analysis_Rana` (currently empty).

---

## Pipeline Overview

```
START
  └─> fetch_repo
        └─> identify_abstractions         (LLM: pick 5–N core concepts)
              └─> analyze_relationships   (LLM: directed edges between concepts)
                    └─> order_chapters    (LLM: pedagogical ordering)
                          └─> [Send] -> write_chapter_single (×N, parallel)
                                └─> combine_tutorial (programmatic Mermaid + file writes)
                                      └─> END
```

State is a single `TypedDict`. The `chapters` field uses an `Annotated[List, operator.add]` reducer so parallel `Send` writes merge cleanly.

---

## Directory Layout

```
Repo Analysis_Rana/
├── README.md                      # Usage, design overview, env vars
├── pyproject.toml                 # Deps: langgraph, pyyaml, requests,
│                                  #   google-genai, anthropic, openai
├── requirements.txt
├── .env.example                   # LLM_PROVIDER, *_MODEL, *_API_KEY, *_BASE_URL
├── src/codebase_kb/
│   ├── __init__.py
│   ├── __main__.py                # `python -m codebase_kb` entry
│   ├── cli.py                     # argparse (see CLI surface)
│   ├── config.py                  # Env loader
│   ├── state.py                   # KnowledgeBuilderState TypedDict + reducers
│   ├── graph.py                   # build_graph(): StateGraph + Send fan-out
│   ├── llm/
│   │   ├── base.py                # LLMProvider Protocol
│   │   ├── gemini.py              # google-genai
│   │   ├── anthropic.py           # anthropic SDK
│   │   ├── ollama.py              # OpenAI-compatible, base_url=http://localhost:11434/v1
│   │   └── openai_compat.py       # openai SDK, base_url injectable
│   ├── cache.py                   # DiskCache (sha256(prompt+model) -> JSON)
│   ├── crawler/
│   │   ├── github.py              # GitHub API: trees + contents
│   │   ├── local.py               # os.walk + include/exclude + size cap
│   │   └── models.py              # FileEntry dataclass
│   ├── extract/
│   │   ├── python_ast.py          # ast-based summary block appended to content
│   │   ├── dispatcher.py          # language -> extractor
│   │   └── serialize.py
│   ├── prompts/
│   │   ├── identify.md            # YAML: list of {name, description, file_indices}
│   │   ├── analyze.md             # YAML: {summary, relationships: [{from,to,label}]}
│   │   ├── order.md               # YAML: ordered list of concept names
│   │   └── write_chapter.md       # Markdown chapter w/ Mermaid sequenceDiagram
│   ├── nodes/
│   │   ├── fetch_repo.py
│   │   ├── identify_abstractions.py
│   │   ├── analyze_relationships.py
│   │   ├── order_chapters.py
│   │   ├── write_chapters.py      # write_chapter_single (Send target)
│   │   └── combine_tutorial.py
│   ├── output/
│   │   ├── paths.py
│   │   ├── writer.py
│   │   └── mermaid.py             # build_overview_diagram / build_chapter_sequence
│   └── utils/
│       ├── yaml_parse.py          # safe extract from ```yaml fences
│       └── hashing.py
├── tests/
│   ├── fixtures/tiny_repo/        # 5-file Python toy
│   ├── test_graph.py              # end-to-end on fixture
│   ├── test_crawler.py
│   ├── test_extract_python.py
│   ├── test_mermaid.py
│   ├── test_cache.py
│   └── test_cli.py
└── docs/design.md                 # mirrors README "Design" section
```

---

## State Shape (`src/codebase_kb/state.py`)

```python
class KnowledgeBuilderState(TypedDict, total=False):
    # --- inputs (set once by CLI) ---
    repo_url: Optional[str]
    local_dir: Optional[str]
    project_name: str
    github_token: Optional[str]
    output_dir: str
    include_patterns: List[str]
    exclude_patterns: List[str]
    max_file_size: int
    language: str
    max_abstractions: int
    use_cache: bool

    # --- intermediate ---
    files: List[Dict[str, str]]         # [{"path": ..., "content": "<source + ast summary>"}]
    abstractions: List[Dict[str, Any]]  # [{name, description, file_indices: [int,...]}]
    relationships: List[Dict[str, Any]] # [{from, to, label}]  (parsed from YAML)
    chapter_order: List[int]            # ordered abstraction indices

    # --- outputs ---
    chapters: List[Dict[str, Any]]      # [{index, name, markdown}, ...] (reducer-merged)
    final_output_dir: str
```

`chapters` is declared `Annotated[List[Dict[str, Any]], operator.add]` so `Send` workers can each return `{"chapters": [single]}` and the framework concatenates.

---

## StateGraph Wiring (`src/codebase_kb/graph.py`)

```python
from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send

def build_graph():
    g = StateGraph(KnowledgeBuilderState)
    g.add_node("fetch_repo",            fetch_repo_node)
    g.add_node("identify_abstractions", identify_abstractions_node)
    g.add_node("analyze_relationships", analyze_relationships_node)
    g.add_node("order_chapters",        order_chapters_node)
    g.add_node("write_chapter_single",  write_chapter_single)   # Send target
    g.add_node("combine_tutorial",      combine_tutorial_node)

    g.add_edge(START,                   "fetch_repo")
    g.add_edge("fetch_repo",            "identify_abstractions")
    g.add_edge("identify_abstractions", "analyze_relationships")
    g.add_edge("analyze_relationships", "order_chapters")
    g.add_conditional_edges(
        "order_chapters",
        route_to_chapter_writers,      # -> List[Send] over chapter_order
        ["write_chapter_single"],
    )
    g.add_edge("write_chapter_single",  "combine_tutorial")
    g.add_edge("combine_tutorial",      END)
    return g.compile()
```

`route_to_chapter_writers(state)`: for each `idx` in `state["chapter_order"]`, emit `Send("write_chapter_single", WriteChapterInput(abstraction_index=idx, ...))`.

**Node read/write matrix**:

| Node | Reads | Writes |
|---|---|---|
| fetch_repo | repo_url, local_dir, github_token, include/exclude_patterns, max_file_size | files |
| identify_abstractions | files, language, max_abstractions | abstractions |
| analyze_relationships | abstractions, files | relationships |
| order_chapters | abstractions, relationships | chapter_order |
| write_chapter_single | Send payload | chapters (append) |
| combine_tutorial | chapters, abstractions, relationships, output_dir, project_name | (filesystem writes; final_output_dir) |

---

## Python AST Extraction (`src/codebase_kb/extract/python_ast.py`)

Goal: keep full file content for LLM context, but prepend a compact structural summary to save tokens and improve signal.

`extract_python(path, source) -> str` returns source + an appended header:

```
# Module: utils.py
# Imports (3): from __future__ import annotations | import json | from typing import Any
# Functions (2): load_config(path: str) -> dict | merge(a: dict, b: dict) -> dict
# Classes (1): Cache(bases=object) methods=get,set,clear
# Decorators (1): @staticmethod merge_into(target, *sources)
# Cross-file call refs: load_config, json.dumps
```

Built from `ast.parse(source)`, walking `ast.Module.body` for `Import`/`ImportFrom`/`FunctionDef`/`AsyncFunctionDef`/`ClassDef`, with `ast.unparse(node.bases)` for class bases and a name-resolution heuristic over `ast.Call.func` for cross-file call candidates. Non-Python files pass through unchanged.

---

## LLM Provider Abstraction

```python
# llm/base.py
class LLMProvider(Protocol):
    def complete(self, prompt: str, *, temperature: float = 0.2,
                 max_tokens: int = 4096) -> str: ...
```

Env-driven dispatch (`llm/__init__.py:get_provider()`):

| Variable | Example |
|---|---|
| `LLM_PROVIDER` | `gemini` \| `anthropic` \| `ollama` \| `openai_compat` |
| `<PROVIDER>_MODEL` | `ANTHROPIC_MODEL=claude-sonnet-4-5` |
| `<PROVIDER>_BASE_URL` | `OLLAMA_BASE_URL=http://localhost:11434/v1` |
| `<PROVIDER>_API_KEY` | required for gemini/anthropic/openai_compat; dummy for ollama |

Provider impls: `GeminiProvider` (google-genai), `AnthropicProvider` (anthropic SDK), `OpenAICompatProvider` (openai SDK with `base_url`), `OllamaProvider` (alias to OpenAICompat with default base). All wrap a single `complete(prompt) -> str` and a unified `LLMCallError` for cache + retry logic at the node layer.

---

## Prompt Strategy

All prompts live in `src/codebase_kb/prompts/*.md` as Markdown templates with `{{var}}` placeholders. Each instructs the model to emit exactly one fenced YAML/Markdown block, parsed by `utils/yaml_parse.py:extract_yaml(response)`.

| Prompt | Output shape | Drives |
|---|---|---|
| `identify.md` | YAML list of `{name, description, file_indices: [int,...]}` (≤ `max_abstractions`) | `abstractions` |
| `analyze.md` | YAML `{summary, relationships: [{from, to, label}]}` — every abstraction must appear in ≥1 edge | `relationships` |
| `order.md` | YAML ordered list of concept names (prereqs first) — mapped to indices | `chapter_order` |
| `write_chapter.md` | Markdown tutorial: motivation, key code excerpts with `file:line`, Mermaid `sequenceDiagram` (≤5 participants), transition links to prev/next chapter | `chapters[i].markdown` |

All prompts accept `{{language}}` for non-English output (paraphrase name/description/summary/labels/whole chapter as required).

---

## Mermaid Generation (`src/codebase_kb/output/mermaid.py`)

**`build_overview_diagram(abstractions, relationships) -> str`**:
```
flowchart TD
    A["Authentication"]
    B["Session Store"]
    A -->|"issues token for"| B
```

**`build_chapter_sequence(chapter, neighbors, relationships) -> str`**:
```
sequenceDiagram
    participant Caller
    participant Auth as Authentication
    participant Store as Session Store
    Caller->>Auth: login(creds)
    Auth->>Store: get(user_id)
    Store-->>Auth: session
    Auth-->>Caller: token
```

**Sanitization rules** (applied identically):
1. Node IDs: `re.sub(r"[^A-Za-z0-9_]", "_", name)[:40]`; prefix `n` if leading digit; numeric suffix on collision.
2. Labels: escape `"` → `#quot;`, collapse `\n` → space, truncate to 60 chars + `…`; HTML-escape `<>|&`.
3. Edge labels: same rules; non-empty fallback `"uses"`.
4. Reserved-keyword guard: `end|subgraph|graph|class` → append `_node`.
5. Validation: regex check on first non-empty line; raise `MermaidGenError` and write the chapter without the diagram on failure (don't abort the pipeline).

---

## Caching Layer (`src/codebase_kb/cache.py`)

`DiskCache(root=".cache/codebase_kb")` — `key = sha256(model + "\x00" + prompt)[:32]`. Stores JSON `{prompt_hash, model, response, ts}`. Atomic writes via tmp-rename. Lookup order in every node: `if state["use_cache"] and cache.get(key): return cached`. `--no-cache` flips `use_cache=False` and bypasses both get and set. Root configurable via `KB_CACHE_DIR`.

---

## Crawler Design

- `crawler/github.py`: `GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1` → for each blob under size cap and matching include/exclude, `GET /contents/{path}` (base64 decode) or fall back to `raw.githubusercontent.com`. Bearer auth via `--token`; respects `X-RateLimit-Remaining`.
- `crawler/local.py`: `os.walk` + `fnmatch` against include/exclude; skip binaries (NUL byte in first 8KB); skip files > `max_file_size`.
- Both return `List[FileEntry(path, content)]` with `path` relative to repo root.

---

## CLI Surface (`src/codebase_kb/cli.py`)

```
python -m codebase_kb
  --repo URL | --dir PATH                  (mutually exclusive, required)
  [--name NAME]                            (default: derived from URL/path)
  [--token TOKEN]                          (env: GITHUB_TOKEN)
  [--output DIR]                           (default: ./output)
  [--include PATTERN ...]                  (default: *)
  [--exclude PATTERN ...]                  (default: heavy ignores)
  [--max-size BYTES]                       (default: 100000)
  [--language LANG]                        (default: english)
  [--max-abstractions N]                   (default: 15)
  [--no-cache]
  [--provider NAME]                        (env: LLM_PROVIDER; default: gemini)
  [--model NAME]                           (env: <PROVIDER>_MODEL)
```

`argparse` enforces `--repo`/`--dir` mutual exclusion. Unknown provider → `SystemExit` with setup hint pointing to `.env.example`.

---

## Critical Files to Create

- `src/codebase_kb/state.py` — `KnowledgeBuilderState` TypedDict + reducers
- `src/codebase_kb/graph.py` — `build_graph()` with `Send` fan-out
- `src/codebase_kb/nodes/fetch_repo.py`
- `src/codebase_kb/nodes/identify_abstractions.py`
- `src/codebase_kb/nodes/analyze_relationships.py`
- `src/codebase_kb/nodes/order_chapters.py`
- `src/codebase_kb/nodes/write_chapters.py` — `write_chapter_single` (Send target)
- `src/codebase_kb/nodes/combine_tutorial.py`
- `src/codebase_kb/llm/base.py` + 4 provider modules
- `src/codebase_kb/cache.py`
- `src/codebase_kb/crawler/{github,local,models}.py`
- `src/codebase_kb/extract/python_ast.py`
- `src/codebase_kb/output/mermaid.py`
- `src/codebase_kb/cli.py`
- `src/codebase_kb/prompts/*.md` (4 files)
- `tests/fixtures/tiny_repo/` (5-file Python toy) + `tests/test_*.py`
- `README.md`, `docs/design.md`, `pyproject.toml`, `requirements.txt`, `.env.example`

---

## Verification

**Fixture test** (`tests/fixtures/tiny_repo`, 5 Python files: `main.py`, `utils.py`, `models.py`, `service.py`, `README.md`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill LLM_PROVIDER + key
python -m codebase_kb \
  --dir tests/fixtures/tiny_repo \
  --name tiny_repo \
  --output ./output \
  --max-abstractions 5 \
  --include "*.py" \
  --max-size 50000
```

**Expected output**:
```
output/tiny_repo/
├── index.md                 # overview Mermaid flowchart + numbered TOC
├── 01_<concept_a>.md        # each contains a sequenceDiagram
├── 02_<concept_b>.md
└── ...
```

**Automated assertions** (`tests/test_graph.py`):
1. `index.md` exists, starts with `# Tiny_repo Code Knowledge`, contains a `\`\`\`mermaid` block with `flowchart TD`.
2. Every chapter file exists, numbered sequentially, slug matches its title, contains ≥1 fenced code block referencing the fixture, and contains a `sequenceDiagram` block.
3. For every `from → to` edge in `relationships`, both abstractions have chapter files.
4. `chapter_order` is a permutation of `abstractions`.
5. Re-running with cache enabled produces byte-identical output (determinism).
6. Re-running with `--no-cache` is measurably slower (cache actually being hit).

**Manual smoke test** (documented, not CI):
```bash
python -m codebase_kb --repo https://github.com/<owner>/<small-repo> \
  --name smoke --token $GH_TOKEN --max-abstractions 8
```
Open `output/smoke/index.md` in a Mermaid-enabled viewer; confirm diagram renders and chapter links resolve.

**Negative paths to verify**:
- LLM returns malformed YAML → node raises → framework retries (max 3) → surfaces clear error.
- All providers missing env vars → `SystemExit` at startup with actionable message.
- Empty repo / zero files → `FetchRepo` raises; CLI exits non-zero.
- `--repo` and `--dir` both passed → argparse rejects with usage.
- Cache corruption → fall back to live call, log warning.
