# Automated Codebase Knowledge Builder — Detailed Plan (Teacher Edition)

## What You Are Building

A Python CLI tool that ingests a code repository (Git URL or local path), reverse-engineers its architecture, and emits a structured Markdown tutorial (`index.md` + numbered chapter files) with auto-generated Mermaid diagrams.

**Why it matters**: Onboarding to large, undocumented repos is one of the most expensive bottlenecks in software engineering. New developers waste days manually tracing imports and reading files line-by-line. Existing tools fail differently — wikis go stale, docstring generators are too granular, semantic search (Bloop, Sourcegraph) requires the developer to already know what to look for. This tool builds the mental map automatically.

**Reference architecture**: [PocketFlow-Tutorial-Codebase-Knowledge](https://the-pocket.github.io/PocketFlow-Tutorial-Codebase-Knowledge/) — a 6-node linear pipeline (Fetch → Identify → Analyze → Order → Write (BatchNode) → Combine) re-implemented in **LangGraph** with a pluggable LLM layer.

**Working directory**: `/home/debanuj/Desktop/Repo Analysis_Rana` (currently empty).

---

## The Big Picture: Pipeline Mental Model

Think of the tool as an assembly line. Each "node" is a worker with a clipboard (the `State`). The worker reads the clipboard, does one job, writes the result back, and passes it on. The last few workers run in parallel because they are independent.

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

**The 6 nodes, in plain English:**

1. **fetch_repo** — Go grab the code. Either clone via GitHub API or walk a local directory. Skip binaries, skip giant files, respect include/exclude patterns.
2. **identify_abstractions** — Ask the LLM: "What are the 5–15 core concepts in this codebase?" Output: a list of `{name, description, file_indices}`.
3. **analyze_relationships** — Ask the LLM: "How do these concepts depend on each other?" Output: directed edges `[{from, to, label}]`.
4. **order_chapters** — Ask the LLM: "What is the best teaching order? Prereqs first." Output: a permutation of concept names.
5. **write_chapter_single** (×N, parallel) — For each concept, ask the LLM: "Write a tutorial chapter explaining this concept with code excerpts and a Mermaid sequence diagram." Each chapter is written in parallel.
6. **combine_tutorial** — Programmatically stitch everything together: build the overview Mermaid `flowchart TD` from the validated edges, generate the `index.md` table of contents, write all files to disk. This step is **not** LLM-driven — diagrams are built from validated LLM output, guaranteeing accuracy.

---

## Core Concept 1: LangGraph StateGraph

A `StateGraph` is a workflow engine where data flows through nodes via a shared `TypedDict`. Think of it as a conveyor belt: each station takes the box, modifies it, and passes it on.

**Key trick: `Send`**. When `order_chapters` finishes, we don't know in advance how many chapters to write — the LLM decides. The `Send` primitive lets one node dynamically spawn N parallel workers, each with its own mini-payload. The `chapters` field uses a **reducer** (`Annotated[List, operator.add]`) so all parallel writes merge cleanly into one list. Without the reducer, last write wins and you lose chapters.

**Why parallel chapter writing matters**: A repo with 10 concepts means 10 LLM calls. Sequential = 10× latency. Parallel = 1× latency. For a 2-minute-per-call LLM, this saves ~18 minutes per repo.

---

## Directory Layout — Why Each Folder Exists

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
│   ├── cli.py                     # argparse (user-facing)
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

**Folder-by-folder reasoning:**

- `cli.py` / `__main__.py` / `config.py` — the entry layer. User types a command, argparse parses it, env vars are loaded, the state is initialized.
- `state.py` — defines the **shape** of the shared clipboard. Every node reads from and writes to fields here. Using `TypedDict` with `total=False` means fields are optional and added incrementally as the pipeline progresses.
- `graph.py` — defines the **flow** itself: which node runs next, when to fork, when to join. This is the only file that knows the order of operations.
- `nodes/` — one file per pipeline stage. Each node is a pure function: takes state in, returns partial state out. This makes them easy to test in isolation.
- `llm/` — the **pluggable LLM layer**. All providers implement a common `LLMProvider` Protocol with one method: `complete(prompt) -> str`. Switching from Gemini to Anthropic is a one-env-var change. The `OllamaProvider` is just an alias to `OpenAICompatProvider` with a default base URL because Ollama exposes an OpenAI-compatible API.
- `cache.py` — disk-based response cache. Key = `sha256(model + "\x00" + prompt)[:32]`. Re-running the tool on the same repo with the same model should be byte-identical (test #5) and fast (test #6).
- `crawler/` — two implementations of the same interface (`List[FileEntry]`). `github.py` uses the Git Trees API for discovery then the Contents API for file content. `local.py` uses `os.walk` + `fnmatch`. Both skip binaries (NUL byte check) and oversize files.
- `extract/` — language-specific code summarization. The Python extractor uses `ast` (the standard library abstract syntax tree module) to generate a compact structural summary (imports, functions, classes, decorators) that gets prepended to file content. This gives the LLM a "table of contents" for each file without burning tokens on the full source.
- `prompts/` — the four prompt templates. Stored as Markdown with `{{var}}` placeholders. Each instructs the model to emit exactly one fenced YAML/Markdown block, parsed by `utils/yaml_parse.py:extract_yaml()`.
- `output/` — file writing and Mermaid generation. The `mermaid.py` module is **critical**: it sanitizes LLM-provided names into valid Mermaid identifiers and builds the diagrams programmatically. This is why the diagrams are "guaranteed-accurate" — the LLM only provides labels and structure, but the diagram syntax is generated by code that validates every node ID and edge label.
- `utils/` — small helpers. `yaml_parse.py` is the only safe way to consume LLM output: it finds the first `\`\`\`yaml` fence, extracts content, parses with `yaml.safe_load`. This is more robust than asking the LLM for "just YAML" because it tolerates prose before/after the fence.

---

## State Shape — The Shared Clipboard

`src/codebase_kb/state.py`:

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

**Key idea**: `chapters` is declared `Annotated[List[Dict[str, Any]], operator.add]` so `Send` workers can each return `{"chapters": [single]}` and the framework concatenates automatically. This is the **reducer pattern** — the field's update function is `add` instead of `overwrite`.

**Why `file_indices` instead of file paths**: Identifiers in the LLM prompt are integers (1, 2, 3 …) because they're shorter and more robust to special characters. The `files` list preserves order, so `file_indices=[3, 7]` maps to `files[3]` and `files[7]`.

---

## StateGraph Wiring

`src/codebase_kb/graph.py`:

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

**`route_to_chapter_writers(state)`**: for each `idx` in `state["chapter_order"]`, emit `Send("write_chapter_single", WriteChapterInput(abstraction_index=idx, ...))`. LangGraph automatically waits for all `Send` workers to finish before proceeding to `combine_tutorial`.

### Node Read/Write Matrix

| Node | Reads | Writes |
|---|---|---|
| fetch_repo | repo_url, local_dir, github_token, include/exclude_patterns, max_file_size | files |
| identify_abstractions | files, language, max_abstractions | abstractions |
| analyze_relationships | abstractions, files | relationships |
| order_chapters | abstractions, relationships | chapter_order |
| write_chapter_single | Send payload | chapters (append via reducer) |
| combine_tutorial | chapters, abstractions, relationships, output_dir, project_name | filesystem writes; final_output_dir |

This matrix is your **debugging tool**: if a node is producing garbage, check what it read. If a downstream node is missing data, check what the upstream wrote.

---

## Python AST Extraction — Token-Saving Trick

`src/codebase_kb/extract/python_ast.py`:

**Goal**: keep full file content for LLM context, but prepend a compact structural summary to save tokens and improve signal.

`extract_python(path, source) -> str` returns source + an appended header:

```
# Module: utils.py
# Imports (3): from __future__ import annotations | import json | from typing import Any
# Functions (2): load_config(path: str) -> dict | merge(a: dict, b: dict) -> dict
# Classes (1): Cache(bases=object) methods=get,set,clear
# Decorators (1): @staticmethod merge_into(target, *sources)
# Cross-file call refs: load_config, json.dumps
```

**How it works**: `ast.parse(source)` produces an AST (a tree of nodes representing the code). Walk `ast.Module.body` looking for `Import`, `ImportFrom`, `FunctionDef`, `AsyncFunctionDef`, `ClassDef`. Use `ast.unparse(node.bases)` to get string representations of class bases. For cross-file call refs, do a name-resolution heuristic over `ast.Call.func` — find every function call, take the leftmost name, and if it doesn't match a local definition, it's likely a cross-file reference.

**Why this matters**: A 500-line file becomes 500 lines + 5 lines of summary. The LLM gets the full source for code excerpts but uses the summary for orientation ("this file has 3 classes, 12 functions, depends on `requests`"). This dramatically improves the quality of concept identification without burning extra tokens on the full file multiple times.

Non-Python files pass through unchanged — the dispatcher in `extract/dispatcher.py` routes by file extension.

---

## LLM Provider Abstraction — Pluggable Backend

`src/codebase_kb/llm/base.py`:

```python
class LLMProvider(Protocol):
    def complete(self, prompt: str, *, temperature: float = 0.2,
                 max_tokens: int = 4096) -> str: ...
```

**Why a Protocol**: Python's structural typing means any class with a matching `complete` method satisfies the protocol — no inheritance required. This keeps provider implementations decoupled.

**Env-driven dispatch** (`llm/__init__.py:get_provider()`):

| Variable | Example |
|---|---|
| `LLM_PROVIDER` | `gemini` \| `anthropic` \| `ollama` \| `openai_compat` |
| `<PROVIDER>_MODEL` | `ANTHROPIC_MODEL=claude-sonnet-4-5` |
| `<PROVIDER>_BASE_URL` | `OLLAMA_BASE_URL=http://localhost:11434/v1` |
| `<PROVIDER>_API_KEY` | required for gemini/anthropic/openai_compat; dummy for ollama |

**The naming convention `<PROVIDER>_MODEL`** lets a single `.env` file hold configs for all providers — only the one matching `LLM_PROVIDER` is actually read. Switching providers is one env var change.

**Provider impls**:
- `GeminiProvider` — uses `google-genai` SDK.
- `AnthropicProvider` — uses `anthropic` SDK (Claude).
- `OpenAICompatProvider` — uses `openai` SDK with `base_url` injectable (works with any OpenAI-compatible endpoint).
- `OllamaProvider` — alias to `OpenAICompatProvider` with default base URL `http://localhost:11434/v1`. Local inference, no API key needed.

All wrap a single `complete(prompt) -> str` and a unified `LLMCallError` for cache + retry logic at the node layer. **Retry logic lives in the node, not the provider**, because retry policy is a pipeline concern, not a transport concern.

---

## Prompt Strategy — LLM as Structured Output Generator

All prompts live in `src/codebase_kb/prompts/*.md` as Markdown templates with `{{var}}` placeholders. Each instructs the model to emit exactly one fenced YAML/Markdown block, parsed by `utils/yaml_parse.py:extract_yaml(response)`.

| Prompt | Output shape | Drives |
|---|---|---|
| `identify.md` | YAML list of `{name, description, file_indices: [int,...]}` (≤ `max_abstractions`) | `abstractions` |
| `analyze.md` | YAML `{summary, relationships: [{from, to, label}]}` — every abstraction must appear in ≥1 edge | `relationships` |
| `order.md` | YAML ordered list of concept names (prereqs first) — mapped to indices | `chapter_order` |
| `write_chapter.md` | Markdown tutorial: motivation, key code excerpts with `file:line`, Mermaid `sequenceDiagram` (≤5 participants), transition links to prev/next chapter | `chapters[i].markdown` |

**Why fenced YAML**: LLMs are chatty. If you ask for "just YAML," they often add preamble ("Here's the analysis:") or postamble ("Let me know if you need more!"). The `\`\`\`yaml` fence is a strong signal — `extract_yaml()` finds the first fence, extracts content, and parses. This is more robust than regex on "starts with `{`."

**Why `file_indices` instead of paths**: Integer indices are shorter, more robust to special characters in paths (spaces, slashes, unicode), and the order is guaranteed by the `files` list. The prompt shows the LLM a numbered file list once; the LLM references numbers in its response.

**The 5-participant cap on sequence diagrams**: Mermaid sequence diagrams get unreadable fast. 5 participants = 10 possible arrows max. Beyond that, the diagram becomes spaghetti. If a concept has more than 5 collaborators, the prompt instructs the LLM to pick the most important 5.

**Language support**: All prompts accept `{{language}}` for non-English output. This paraphrases names, descriptions, summaries, labels, and whole chapters as required. Implementation: a `language` config in the prompt template that's substituted at runtime.

---

## Mermaid Generation — Why It's Programmatic

`src/codebase_kb/output/mermaid.py`:

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

**The diagrams are NOT emitted by the LLM**. The LLM provides labels and structure (which edges exist, what they're labeled), but the actual Mermaid syntax is generated by `mermaid.py`. This is the key design decision that makes diagrams "guaranteed-accurate."

### Sanitization Rules (Applied Identically to Both Functions)

1. **Node IDs**: `re.sub(r"[^A-Za-z0-9_]", "_", name)[:40]`; prefix `n` if leading digit; numeric suffix on collision. Mermaid node IDs must match `[A-Za-z_][A-Za-z0-9_]*` — spaces, hyphens, dots, and unicode all break parsing.
2. **Labels**: escape `"` → `#quot;`, collapse `\n` → space, truncate to 60 chars + `…`; HTML-escape `<>|&`. Mermaid labels are quoted strings; embedded quotes terminate the string early.
3. **Edge labels**: same rules; non-empty fallback `"uses"`. Empty edge labels look broken in Mermaid.
4. **Reserved-keyword guard**: `end|subgraph|graph|class` → append `_node`. Mermaid reserves these as structural keywords — using them as node IDs causes parse errors.
5. **Validation**: regex check on first non-empty line; raise `MermaidGenError` and write the chapter without the diagram on failure. **Don't abort the pipeline** — a broken diagram shouldn't kill an otherwise valid tutorial.

**Why the "don't abort" rule**: Mermaid syntax is finicky. If one chapter's diagram fails validation, the user still gets 14 other valid chapters. Better to degrade gracefully than to lose the whole tutorial.

---

## Caching Layer — Determinism + Speed

`src/codebase_kb/cache.py`:

`DiskCache(root=".cache/codebase_kb")` — `key = sha256(model + "\x00" + prompt)[:32]`. Stores JSON `{prompt_hash, model, response, ts}`. Atomic writes via tmp-rename.

**Lookup order in every node**: `if state["use_cache"] and cache.get(key): return cached`. `--no-cache` flips `use_cache=False` and bypasses both get and set.

**Root configurable via `KB_CACHE_DIR`** for CI environments that mount specific paths.

**Why cache by `model + prompt`**: Different models produce different responses for the same prompt. Hashing both ensures you never serve a Gemini response when an Anthropic call is expected. The `"\x00"` separator prevents `("gpt-4", "abc")` from colliding with `("gpt-4abc", "")`.

**Atomic writes via tmp-rename**: Write to `key.json.tmp`, then `os.rename` to `key.json`. This prevents corrupted cache files if the process is killed mid-write (you either get the old file or the new file, never a half-written one).

**This is what enables test #5 (byte-identical re-runs) and test #6 (cache actually faster)**. Without caching, every CI run is a fresh LLM call — slow, expensive, and non-deterministic.

---

## Crawler Design — Two Implementations, One Interface

- `crawler/github.py`: `GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1` → for each blob under size cap and matching include/exclude, `GET /contents/{path}` (base64 decode) or fall back to `raw.githubusercontent.com`. Bearer auth via `--token`; respects `X-RateLimit-Remaining`.
- `crawler/local.py`: `os.walk` + `fnmatch` against include/exclude; skip binaries (NUL byte in first 8KB); skip files > `max_file_size`.
- Both return `List[FileEntry(path, content)]` with `path` relative to repo root.

**Why two implementations**: Same data shape (`List[FileEntry]`), different sources. The pipeline doesn't care which — it just calls `fetch_files(state)` and gets back a list. This is the **strategy pattern** in action.

**Why the GitHub Trees API first**: One call returns the entire repo structure (all paths, all sizes, all types). Then you only fetch content for files that pass your filters. This minimizes API calls and respects rate limits.

**Why respect `X-RateLimit-Remaining`**: GitHub's API has a 5000 requests/hour limit for authenticated users. If you're 100 requests away from the limit, slow down or back off. The crawler should check this header and sleep if needed.

**Binary detection**: Read first 8KB, check for NUL byte. If present, it's binary (image, compiled object, etc.) — skip it. LLMs can't process binary content and it'd blow up your context.

---

## CLI Surface — What the User Types

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

**`argparse` enforces `--repo`/`--dir` mutual exclusion**. Unknown provider → `SystemExit` with setup hint pointing to `.env.example`.

**Why mutually exclusive**: One source of files. If both are passed, the user made a mistake — reject with usage error.

**Why default excludes**: Standard ignores for `node_modules/`, `.git/`, `__pycache__/`, `*.pyc`, `dist/`, `build/`, `.venv/`, etc. Most users don't want to index these. Override with `--include` if needed.

**Why `--max-size` default 100KB**: Most source files are under 50KB. Files larger than 100KB are often generated (lock files, minified bundles, large data fixtures) — not useful for understanding architecture.

---

## Critical Files to Create (In Build Order)

1. `pyproject.toml` + `requirements.txt` + `.env.example` — project skeleton.
2. `src/codebase_kb/__init__.py` + `__main__.py` — package init.
3. `src/codebase_kb/state.py` — `KnowledgeBuilderState` TypedDict + reducers.
4. `src/codebase_kb/config.py` — env loader.
5. `src/codebase_kb/utils/{yaml_parse.py, hashing.py}` — small helpers used everywhere.
6. `src/codebase_kb/llm/base.py` + 4 provider modules — LLM abstraction.
7. `src/codebase_kb/cache.py` — disk cache.
8. `src/codebase_kb/crawler/{models.py, github.py, local.py}` — file fetching.
9. `src/codebase_kb/extract/{python_ast.py, dispatcher.py, serialize.py}` — code summarization.
10. `src/codebase_kb/prompts/{identify, analyze, order, write_chapter}.md` — prompt templates.
11. `src/codebase_kb/output/{paths.py, writer.py, mermaid.py}` — file writing and diagrams.
12. `src/codebase_kb/nodes/{fetch_repo, identify_abstractions, analyze_relationships, order_chapters, write_chapters, combine_tutorial}.py` — the 6 workers.
13. `src/codebase_kb/graph.py` — wires it all together.
14. `src/codebase_kb/cli.py` — argparse + entry point.
15. `tests/fixtures/tiny_repo/` (5 Python files) + `tests/test_*.py` — verification.
16. `README.md` + `docs/design.md` — user-facing docs.

**Why this order**: each file depends on the ones above it. `state.py` is used by everything, so it comes first. Providers are used by nodes, so they come before nodes. The graph is the last "code" file because it imports from all nodes.

---

## How to Build This (Step-by-Step)

### Phase 1: Project Skeleton
1. Create the folder structure.
2. Write `pyproject.toml` with deps: `langgraph`, `pyyaml`, `requests`, `google-genai`, `anthropic`, `openai`.
3. Write `requirements.txt` (pip-compatible, no extras).
4. Write `.env.example` with all env var names and a comment explaining each.
5. Initialize git, make first commit.

### Phase 2: State + Utils
1. Define `KnowledgeBuilderState` in `state.py`. Add the `Annotated` reducer for `chapters`.
2. Implement `yaml_parse.py:extract_yaml()` — find first `\`\`\`yaml` fence, extract, parse.
3. Implement `hashing.py:sha256_key()` — return first 32 chars of `sha256(s.encode()).hexdigest()`.

### Phase 3: LLM Layer
1. Define `LLMProvider` Protocol in `llm/base.py`.
2. Implement `GeminiProvider` using `google-genai`. Test with a one-line prompt.
3. Implement `AnthropicProvider` using `anthropic` SDK. Test.
4. Implement `OpenAICompatProvider` with `base_url` parameter. Test.
5. Implement `OllamaProvider` as a thin wrapper around `OpenAICompatProvider` with default base URL.
6. Implement `get_provider()` dispatcher in `llm/__init__.py`.

### Phase 4: Cache
1. Implement `DiskCache` class with `get(key)`, `set(key, value)`, `has(key)` methods.
2. Atomic writes via `tempfile.NamedTemporaryFile` + `os.replace`.
3. Add a `--no-cache` test: cache hit should return the exact same bytes as the first call.

### Phase 5: Crawlers
1. Define `FileEntry` dataclass in `crawler/models.py`.
2. Implement `crawler/local.py` first (easier to test — no API).
3. Implement `crawler/github.py` with rate-limit awareness.
4. Add binary detection (NUL byte check) and size cap to both.

### Phase 6: AST Extractor
1. Implement `extract_python(path, source) -> str` using `ast.parse`.
2. Walk `ast.Module.body` for imports, functions, classes.
3. Use `ast.unparse()` for class bases and function signatures.
4. Implement name-resolution heuristic for cross-file call refs.
5. Add unit tests with the tiny_repo fixture.

### Phase 7: Prompts
1. Write `identify.md` — instruct LLM to emit a YAML list with `name`, `description`, `file_indices`. Include the `{{files}}`, `{{max_abstractions}}`, `{{language}}` placeholders.
2. Write `analyze.md` — instruct LLM to emit `{summary, relationships}` with every abstraction in ≥1 edge.
3. Write `order.md` — instruct LLM to emit an ordered list of concept names.
4. Write `write_chapter.md` — instruct LLM to emit a Markdown chapter with motivation, code excerpts (`file:line`), and a Mermaid `sequenceDiagram` (≤5 participants).

### Phase 8: Mermaid Generator
1. Implement `sanitize_id(name) -> str` with all 5 sanitization rules.
2. Implement `build_overview_diagram(abstractions, relationships) -> str`.
3. Implement `build_chapter_sequence(chapter, neighbors, relationships) -> str`.
4. Add validation: regex check on first non-empty line, raise `MermaidGenError` on failure.
5. Unit test with adversarial inputs (unicode, reserved keywords, empty labels).

### Phase 9: Nodes
1. `fetch_repo.py` — call `crawler` (local or GitHub based on state), return `{"files": [...]}`. Cache via `DiskCache`.
2. `identify_abstractions.py` — build prompt from `files`, call LLM, parse YAML, return `{"abstractions": [...]}`.
3. `analyze_relationships.py` — build prompt from `abstractions` + `files`, call LLM, parse YAML, return `{"relationships": [...]}`.
4. `order_chapters.py` — build prompt from `abstractions` + `relationships`, call LLM, map names to indices, return `{"chapter_order": [...]}`.
5. `write_chapters.py` — define `write_chapter_single(Send payload)` that takes one abstraction, calls LLM, returns `{"chapters": [{...}]}`.
6. `combine_tutorial.py` — read all chapters, build overview diagram, write `index.md` + `NN_name.md` files, return `{"final_output_dir": "..."}`.

### Phase 10: Graph + CLI
1. Implement `build_graph()` in `graph.py` — wire all 6 nodes, add `Send` fan-out from `order_chapters`.
2. Implement `cli.py` with argparse — enforce `--repo`/`--dir` mutual exclusion, load config, initialize state, invoke graph.
3. Add `__main__.py` to support `python -m codebase_kb`.

### Phase 11: Tests + Fixture
1. Create `tests/fixtures/tiny_repo/` with 5 Python files: `main.py`, `utils.py`, `models.py`, `service.py`, `README.md`. Make them small but representative (e.g., a CLI tool with config, models, service layer).
2. Write `test_graph.py` with the 6 assertions from the verification section.
3. Write `test_crawler.py` — local walk, GitHub mock with `responses` library.
4. Write `test_extract_python.py` — known input → known AST summary.
5. Write `test_mermaid.py` — adversarial sanitization tests.
6. Write `test_cache.py` — atomicity, key collision, corruption recovery.
7. Write `test_cli.py` — argparse behavior, mutual exclusion, unknown provider.

### Phase 12: Documentation
1. Write `README.md` — usage examples, env var reference, design overview.
2. Write `docs/design.md` — mirrors the "Design" section of README for discoverability.

---

## Verification — How to Know It Works

### Fixture Test (Manual)

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

### Automated Assertions (`tests/test_graph.py`)

1. `index.md` exists, starts with `# Tiny_repo Code Knowledge`, contains a `\`\`\`mermaid` block with `flowchart TD`.
2. Every chapter file exists, numbered sequentially, slug matches its title, contains ≥1 fenced code block referencing the fixture, and contains a `sequenceDiagram` block.
3. For every `from → to` edge in `relationships`, both abstractions have chapter files.
4. `chapter_order` is a permutation of `abstractions`.
5. Re-running with cache enabled produces byte-identical output (determinism).
6. Re-running with `--no-cache` is measurably slower (cache actually being hit).

### Manual Smoke Test (Documented, Not CI)

```bash
python -m codebase_kb --repo https://github.com/<owner>/<small-repo> \
  --name smoke --token $GH_TOKEN --max-abstractions 8
```

Open `output/smoke/index.md` in a Mermaid-enabled viewer; confirm diagram renders and chapter links resolve.

### Negative Paths to Verify

- LLM returns malformed YAML → node raises → framework retries (max 3) → surfaces clear error.
- All providers missing env vars → `SystemExit` at startup with actionable message.
- Empty repo / zero files → `FetchRepo` raises; CLI exits non-zero.
- `--repo` and `--dir` both passed → argparse rejects with usage.
- Cache corruption → fall back to live call, log warning.

---

## Teaching Notes — Why These Design Choices

1. **LangGraph over raw Python orchestration**: The `Send` primitive and reducer pattern are hard to get right by hand. LangGraph handles the join semantics automatically — you don't write a "wait for all chapters" barrier, you just declare the edge.

2. **Pluggable LLM layer**: Lock-in to one provider is a strategic risk. Anthropic rate-limits you, Gemini goes down, you want to switch to local Ollama for sensitive code — all of these should be one env var change. The Protocol-based abstraction costs ~50 lines and saves you a rewrite.

3. **Programmatic Mermaid generation**: LLMs are unreliable at emitting valid syntax for structured formats. Ask an LLM to write Mermaid and you'll get parse errors 20% of the time. Ask it for *labels* and build the syntax yourself — 0% parse errors, same visual output.

4. **Disk cache for LLM responses**: LLM API calls are the bottleneck (seconds to minutes). Caching by `model + prompt` makes the tool feel instant on re-runs and makes tests deterministic. Without this, your CI is non-hermetic and slow.

5. **AST-based extraction**: The LLM doesn't need to re-derive structure from raw source. Pre-computing the AST summary gives the LLM a "table of contents" for each file, improving concept identification quality by ~30% (informal observation from the reference project).

6. **State as TypedDict, not class**: TypedDict gives you type hints + IDE autocomplete without the boilerplate of a class. The `total=False` flag means fields are added incrementally as the pipeline progresses — you don't have to pre-declare every field at init time.

7. **One node per file**: Makes nodes independently testable. You can unit-test `identify_abstractions` by feeding it a fake state with `files` and checking the output structure — no need to run the full pipeline.

8. **Prompts as Markdown files, not Python strings**: Prompts evolve. Keeping them in `.md` files means non-Python developers can edit them, you can syntax-highlight them, and you can version-control diffs cleanly.

---

## Summary

You're building a 6-stage assembly line (LangGraph StateGraph) that turns source code into a Markdown tutorial. The key insights:

- **StateGraph** = conveyor belt with shared clipboard.
- **`Send`** = dynamic parallel fan-out for chapter writing.
- **Pluggable LLM** = Protocol-based abstraction with env-driven dispatch.
- **Programmatic Mermaid** = LLM provides labels, code builds syntax → guaranteed valid diagrams.
- **Disk cache** = deterministic re-runs + fast iteration.
- **AST extraction** = token-efficient structural summaries.
- **Fenced YAML** = robust LLM output parsing.

Build in the order listed, test at each phase with the tiny_repo fixture, and the architecture will hold together.
