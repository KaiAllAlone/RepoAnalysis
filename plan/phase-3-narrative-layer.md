# Phase 3 — Narrative Layer: From Graph To Tutorial

## Where You Are

You have a working **code-intel layer**:

```
repo URL / tarball
   └─> fetch_repo (github.py)         ✅ done
        └─> build_code_graph          ✅ done
             • AST → CodeNode/CodeEdge
             • NetworkX DiGraph
             • PageRank, communities, topo sort, sliced_context
```

After `build_code_graph`, `state["code_graph"]` is a serialized NetworkX graph (currently `nx.node_link_data` output) with no precomputed metrics — just nodes + edges.

**What you don't have**: an LLM, a cache, prompts, state plumbing, or any of the 5 remaining pipeline nodes. So the pipeline stops dead at node 2 and produces no tutorial.

## What This Plan Builds

The **narrative layer** — the pieces that turn a code graph into a Markdown tutorial:

```
build_code_graph  →  state["code_graph"]
   └─> identify_abstractions    (LLM clusters PageRank top-K into 5-15 concepts)
        └─> analyze_relationships  (LLM labels structural edges + adds semantic edges)
             └─> order_chapters     (topo sort, optional LLM rationale)
                  └─> [Send] -> write_chapter_single  (×N parallel, sliced context)
                        └─> combine_tutorial         (programmatic Mermaid + index.md + zip)
```

Plus the cross-cutting infrastructure the LLM nodes need: LLM providers, two-tier cache, prompts, YAML/tokens utils, codeintel deserializer + slicing, and a fixed `output/mermaid.py`.

**Explicitly NOT in this plan** (saved for later phases): auth, DB, REST/SSE API, Arq workers, frontend, billing. We'll wire a **CLI entry point** that invokes the graph directly so you can smoke-test end-to-end without those.

---

## Big-Picture Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          CLI / Run()                                 │
│  - builds initial KnowledgeBuilderState                              │
│  - calls graph.ainvoke(state)                                        │
│  - prints progress to stdout                                         │
└──────────────────────┬───────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   LangGraph StateGraph (graph.py)                    │
│  START → fetch_repo → build_code_graph → identify → analyze → order  │
│                       → [Send] → write_chapter_single (×N)            │
│                       → combine_tutorial → END                        │
└──────────────────────┬───────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Cross-cutting services (DI'd into each node)                        │
│  • LLMProvider (per-org router, pluggable: gemini/anthropic/...)    │
│  • Two-tier cache (Redis L1 + disk L2, per-org)                      │
│  • Prompt renderer (Markdown templates → Jinja-substituted string)   │
│  • CodeGraph deserializer (state["code_graph"] → CodeGraph)          │
│  • Token-budgeted context slicer                                     │
│  • Mermaid builder (programmatic, validated)                         │
│  • YAML / token utilities                                            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Critical File Inventory (In Build Order)

| # | File | Purpose | Deps |
|---|---|---|---|
| 1 | `backend/src/codebase_kb/utils/yaml_parse.py` | Safely extract ```` ```yaml ```` fences | `pyyaml` |
| 2 | `backend/src/codebase_kb/utils/tokens.py` | tiktoken-based token counter | `tiktoken` |
| 3 | `backend/src/codebase_kb/utils/hashing.py` | sha256 keys for cache + dedup | stdlib |
| 4 | `backend/src/codebase_kb/llm/base.py` | `LLMProvider` Protocol + exceptions | none |
| 5 | `backend/src/codebase_kb/llm/gemini.py` | Google Gemini impl | `google-genai` |
| 6 | `backend/src/codebase_kb/llm/anthropic.py` | Anthropic Claude impl | `anthropic` |
| 7 | `backend/src/codebase_kb/llm/openai_compat.py` | OpenAI / Ollama impl | `openai` |
| 8 | `backend/src/codebase_kb/llm/router.py` | Per-org key → provider | DB (or env) |
| 9 | `backend/src/codebase_kb/cache.py` | Two-tier Redis + disk | `redis`, stdlib |
| 10 | `backend/src/codebase_kb/prompts/render.py` | Load + Jinja-substitute `.md` | `jinja2` |
| 11 | `backend/src/codebase_kb/prompts/identify.md` | Template: cluster top-K modules | — |
| 12 | `backend/src/codebase_kb/prompts/analyze.md` | Template: label edges + semantic | — |
| 13 | `backend/src/codebase_kb/prompts/order.md` | Template: optional rationale | — |
| 14 | `backend/src/codebase_kb/prompts/write_chapter.md` | Template: one chapter | — |
| 15 | `backend/src/codebase_kb/codeintel/graph.py` | **Add** `from_payload()` deserializer | `networkx` |
| 16 | `backend/src/codebase_kb/codeintel/slicing.py` | `build_chapter_prompt` (token-budgeted) | `tiktoken` |
| 17 | `backend/src/codebase_kb/output/mermaid.py` | **Rewrite** to new API (`build_overview_diagram`, `build_chapter_sequence`, `build_cycles_diagram`, `MermaidGenError`) | stdlib |
| 18 | `backend/src/codebase_kb/output/writer.py` | Atomic `write_text`, `write_bytes` | stdlib |
| 19 | `backend/src/codebase_kb/output/zip.py` | Deterministic `zip_directory` | stdlib |
| 20 | `backend/src/codebase_kb/graph/state.py` | `KnowledgeBuilderState` TypedDict | stdlib |
| 21 | `backend/src/codebase_kb/graph/nodes/identify_abstractions.py` | Node 3 | all above |
| 22 | `backend/src/codebase_kb/graph/nodes/analyze_relationships.py` | Node 4 | all above |
| 23 | `backend/src/codebase_kb/graph/nodes/order_chapters.py` | Node 5 | all above |
| 24 | `backend/src/codebase_kb/graph/nodes/write_chapters.py` | Node 6 (Send target) | all above |
| 25 | `backend/src/codebase_kb/graph/nodes/combine_tutorial.py` | Node 7 | all above |
| 26 | `backend/src/codebase_kb/graph/graph.py` | StateGraph wiring with `Send` | `langgraph` |
| 27 | `backend/src/codebase_kb/__main__.py` | CLI entry point (argv → invoke graph) | `click` |
| 28 | `backend/tests/unit/test_*.py` | One per file above | `pytest`, `pytest-asyncio` |
| 29 | `backend/tests/integration/test_pipeline_tiny_repo.py` | Full graph run on Q-Learning fixture | `pytest-asyncio` |

**Why this order**: each file is independently testable, and lower-numbered files have no forward deps. Stop and run unit tests after every 3–4 files.

---

## Step 1: Fix `output/mermaid.py` (and align it with the new API)

The current file imports `from matplotlib.cbook import sanitize_sequence` (a dead import — not actually used) and exposes a different API (`generate_flowchart`, `generate_sequence_diagram`) than what the plan calls for. Rewrite to match the plan.

```python
# backend/src/codebase_kb/output/mermaid.py
"""Programmatic Mermaid diagram generation. The LLM provides *labels*; we
generate the syntax. This avoids the ~20% parse-error rate when LLMs emit
Mermaid directly.

Sanitization rules (applied identically to every diagram):
1. Node IDs: re.sub(r"[^A-Za-z0-9_]", "_", name)[:40]; prefix "n" if leading digit;
   numeric suffix on collision.
2. Labels: escape " -> #quot;, collapse newlines, truncate to 60 chars + ellipsis;
   HTML-escape < > | &.
3. Edge labels: same as node labels; fallback "uses" if empty.
4. Reserved-keyword guard: end|subgraph|graph|class -> append "_node".
5. Validation: first non-empty line of result must match one of the allowed
   diagram headers; raise MermaidGenError on failure.

If a diagram fails, the calling node catches MermaidGenError, logs a warning,
and continues without that diagram. The pipeline never fails because Mermaid
felt finicky.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Iterable

# ---------- exceptions ----------
class MermaidGenError(Exception):
    """Raised when a Mermaid diagram cannot be generated or validated."""

# ---------- sanitization primitives ----------
_RESERVED = {"end", "subgraph", "graph", "class", "click", "style",
             "flowchart", "sequenceDiagram"}
_ID_RE = re.compile(r"[^A-Za-z0-9_]")
_USED_IDS: set[str] = set()  # process-local; reset per diagram below


def _reset_ids() -> None:
    global _USED_IDS
    _USED_IDS = set()


def _safe_id(raw: str) -> str:
    """Mermaid crashes on colons, dots, spaces, hyphens in IDs. Sanitize."""
    s = _ID_RE.sub("_", raw)[:40]
    if s and s[0].isdigit():
        s = "n" + s
    if not s:
        s = "n"
    base, n = s, 0
    while s in _USED_IDS:
        n += 1
        s = f"{base}_{n}"
    _USED_IDS.add(s)
    return s


def _safe_label(raw: str, *, max_len: int = 60) -> str:
    """Make a string safe as a Mermaid label: escape quotes, collapse ws,
    truncate, HTML-escape a few metachars."""
    if raw is None:
        return ""
    s = str(raw).replace('"', "#quot;").replace("\n", " ").replace("\r", " ")
    s = s.replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
    s = s.replace("&", "&amp;")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    # Guard against reserved words appearing as a label-only node
    if s.lower() in _RESERVED:
        s += "_node"
    return s or "?"


def _validate_header(text: str, *allowed: str) -> None:
    """First non-empty line must be one of `allowed` headers."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            if not any(s.startswith(a) for a in allowed):
                raise MermaidGenError(f"bad header: {s!r} not in {allowed}")
            return
    raise MermaidGenError("empty diagram")


# ---------- public API ----------
def build_overview_diagram(abstractions: list[dict],
                           relationships: list[dict]) -> str:
    """High-level architecture flowchart.

    abstractions:  [{"name": "Auth", ...}, ...]
    relationships: [{"from": "Auth", "to": "Store", "label": "uses", "kind": "import"}, ...]
    """
    _reset_ids()
    abs_names = {a["name"] for a in abstractions}
    lines = ["flowchart TD"]

    # 1) Declare all nodes (so disconnected nodes still render)
    for a in abstractions:
        nid = _safe_id(a["name"])
        lbl = _safe_label(a.get("description") or a["name"], max_len=80)
        lines.append(f'    {nid}["{lbl}"]')

    # 2) Declare edges with kind-aware arrow styles
    arrow = {
        "import":   "-.->",
        "call":     "-->",
        "inherits": "==>",
        "contains": "-->",
        "semantic": "-.->",
    }
    for r in relationships:
        if r.get("from") not in abs_names or r.get("to") not in abs_names:
            continue
        if r["from"] == r["to"]:
            continue
        s = _safe_id(r["from"])
        d = _safe_id(r["to"])
        kind = (r.get("kind") or "semantic").lower()
        a = arrow.get(kind, "-->")
        lbl = _safe_label(r.get("label") or "uses", max_len=40)
        lines.append(f'    {s} {a}|"{lbl}"| {d}')

    out = "\n".join(lines)
    _validate_header(out, "flowchart")
    return out


def build_chapter_sequence(abstraction: dict,
                           relationships: list[dict],
                           max_participants: int = 5) -> str:
    """Per-chapter sequence diagram showing how this abstraction talks to
    its neighbors (in or out edges)."""
    _reset_ids()
    name = abstraction["name"]
    lines = ["sequenceDiagram"]

    # Participants = this abstraction + its neighbors, capped.
    neighbors: list[str] = []
    for r in relationships:
        for endpoint in (r.get("from"), r.get("to")):
            if endpoint and endpoint != name and endpoint not in neighbors:
                neighbors.append(endpoint)
    participants = [name] + neighbors[: max_participants - 1]
    for p in participants:
        lines.append(f"    participant {_safe_id(p)} as {_safe_label(p)}")

    # Edges from this abstraction to/from its neighbors
    src_id = _safe_id(name)
    for r in relationships:
        if r.get("from") not in participants or r.get("to") not in participants:
            continue
        if r["from"] == r["to"]:
            continue
        s = _safe_id(r["from"])
        d = _safe_id(r["to"])
        lbl = _safe_label(r.get("label") or "uses", max_len=40)
        lines.append(f"    {s}->>+{d}: {lbl}")
        lines.append(f"    {d}-->>-{s}: ok")

    out = "\n".join(lines)
    _validate_header(out, "sequenceDiagram")
    return out


def build_cycles_diagram(cycles: list[list[str]],
                         file_label_fn=None) -> str:
    """Diagram of circular-import / recursive-call cycles (architectural smell).

    Each cycle rendered as a subgraph with a self-loop. Cap at 5 cycles.
    """
    _reset_ids()
    cycles = cycles[:5]
    lines = ["flowchart LR"]
    for i, cyc in enumerate(cycles):
        sid = f"sg{i}"
        lines.append(f"    subgraph {sid} [Cycle {i + 1}]")
        for n in cyc:
            lines.append(f"        {_safe_id(n)}[\"{_safe_label(n)}\"]")
        # close the loop
        for j in range(len(cyc)):
            a = _safe_id(cyc[j])
            b = _safe_id(cyc[(j + 1) % len(cyc)])
            lines.append(f"        {a} --> {b}")
        lines.append("    end")
    out = "\n".join(lines)
    _validate_header(out, "flowchart")
    return out
```

**Verification (eyeball)**: `build_overview_diagram([{"name": "Auth"}, {"name": "Store"}], [{"from": "Auth", "to": "Store", "label": "calls", "kind": "call"}])` → produces a valid `flowchart TD` with two nodes and one edge.

**Unit test** (`tests/unit/test_mermaid.py`): feed adversarial inputs (unicode `αβγ`, reserved word `class`, empty label, very long name, leading digit) — assert no exceptions, IDs/ labels are safe, validators trigger `MermaidGenError` on bad headers.

---

## Step 2: `utils/yaml_parse.py`, `tokens.py`, `hashing.py`

```python
# backend/src/codebase_kb/utils/yaml_parse.py
"""Find the FIRST ```yaml fence in an LLM response and parse it.

The LLM is asked to emit exactly one fenced YAML block; this helper is
tolerant of preamble/postamble chatter.
"""
from __future__ import annotations
import re
import yaml
from typing import Any

_FENCE = re.compile(r"```(?:yaml|yml)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)

def extract_yaml(text: str) -> Any:
    """Returns the parsed YAML object, or None if no fence was found."""
    if not text:
        return None
    m = _FENCE.search(text)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
```

```python
# backend/src/codebase_kb/utils/tokens.py
"""Token counting using tiktoken. We use cl100k_base as a reasonable proxy
for all current models (Anthropic, OpenAI, Gemini via approximation)."""
from __future__ import annotations
import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_ENC.encode(text, disallowed_special=()))


def truncate_to_tokens(text: str, budget: int) -> str:
    """Best-effort truncate. Encodes, slices, decodes."""
    ids = _ENC.encode(text, disallowed_special=())
    if len(ids) <= budget:
        return text
    return _ENC.decode(ids[:budget])
```

```python
# backend/src/codebase_kb/utils/hashing.py
"""Stable, content-addressed hashes for cache keys and dedup."""
from __future__ import annotations
import hashlib
import json
from typing import Any

def sha256_hex(payload: Any) -> str:
    """Stable hash of any JSON-serializable object. Sorts dict keys."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def short_hash(payload: Any, length: int = 32) -> str:
    return sha256_hex(payload)[:length]
```

**Tests**: trivial round-trip tests for each; for `truncate_to_tokens`, assert the output's `estimate_tokens` is ≤ `budget` and ends with the input's tail.

---

## Step 3: LLM Layer

`llm/base.py`:
```python
from __future__ import annotations
from typing import Protocol, Optional

class LLMError(Exception): ...
class LLMRateLimitError(LLMError): ...
class LLMContextLengthError(LLMError): ...
class LLMProvider(Protocol):
    name: str
    default_model: str
    async def complete_async(
        self, prompt: str, *,
        temperature: float = 0.2, max_tokens: int = 4096,
        system: Optional[str] = None,
    ) -> str: ...
```

Each provider implementation (Gemini, Anthropic, OpenAI-compat, Ollama) is ~30–60 lines: instantiate the SDK client in `__init__`, wrap `complete_async` around the SDK's sync call using `asyncio.to_thread` (most LLM SDKs are sync), and map known exceptions to the shared error hierarchy.

**Critical: `complete_async` must be truly async.** If the underlying SDK is synchronous (most are), wrap with `asyncio.to_thread(self._sync_complete, ...)`. If you forget this, your parallel `Send` chapter writes will run serially.

`llm/router.py` (simplified for the no-DB phase — just env-based):
```python
from __future__ import annotations
import os
from typing import Optional
from .base import LLMProvider
from .gemini import GeminiProvider
from .anthropic import AnthropicProvider
from .openai_compat import OpenAICompatProvider, OllamaProvider
from ..config import settings

_FACTORIES = {
    "gemini": lambda model: GeminiProvider(api_key=settings.GEMINI_API_KEY, model=model),
    "anthropic": lambda model: AnthropicProvider(api_key=settings.ANTHROPIC_API_KEY, model=model),
    "openai_compat": lambda model: OpenAICompatProvider(api_key=settings.OPENAI_API_KEY, model=model),
    "ollama": lambda model: OllamaProvider(model=model),  # no key, local
}


def get_provider(provider_name: Optional[str] = None,
                 model: Optional[str] = None) -> LLMProvider:
    """Provider resolution for the no-DB phase. Reads from env settings.
    In a future phase, `provider_name` and `model` will be looked up per-org
    from a `api_keys` table; the signature here is already that-compatible."""
    name = (provider_name or settings.LLM_PROVIDER).lower()
    if name not in _FACTORIES:
        raise ValueError(f"Unknown LLM provider: {name}")
    return _FACTORIES[name](model or _default_model(name))


def _default_model(name: str) -> str:
    return {
        "gemini": "gemini-1.5-pro",
        "anthropic": "claude-sonnet-4-5",
        "openai_compat": "gpt-4o-mini",
        "ollama": "llama3.1",
    }[name]
```

**Tests** (all providers): mark with `pytest.mark.provider`; skip if env var missing. The unit tests use a *fake* provider (a class whose `complete_async` returns a canned string) — see the `test_identify_abstractions.py` sketch in the original plan.

---

## Step 4: Two-Tier Cache

```python
# backend/src/codebase_kb/cache.py
from __future__ import annotations
import json
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Optional, Protocol

class _RedisLike(Protocol):
    async def get(self, k: str) -> Optional[bytes]: ...
    async def set(self, k: str, v: bytes, ex: int = ..., nx: bool = ...) -> bool: ...

# L1: Redis (optional — falls back to disk-only if not configured)
_redis: Optional[_RedisLike] = None

def init_redis(url: str) -> None:
    global _redis
    import redis.asyncio as aioredis
    _redis = aioredis.from_url(url, decode_responses=False)

# L2: disk
L2_ROOT = Path(os.getenv("CACHE_DIR", "./.cache"))

# TTL: 24h for L1; forever for L2 (per-org manual eviction)
L1_TTL_SECONDS = 86_400

def _key(stage: str, payload: Any, org_id: str = "_default") -> str:
    body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    h = hashlib.sha256(body).hexdigest()[:32]
    return f"{org_id}:{stage}:{h}"


async def cache_lookup(stage: str, payload: Any, org_id: str = "_default") -> Optional[dict]:
    key = _key(stage, payload, org_id)

    # L1
    if _redis is not None:
        try:
            raw = await _redis.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass  # never let cache errors break the pipeline

    # L2
    p = L2_ROOT / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return None
    return None


async def cache_store(stage: str, payload: Any, value: dict, org_id: str = "_default") -> None:
    key = _key(stage, payload, org_id)
    blob = json.dumps(value, default=str).encode("utf-8")

    # L1
    if _redis is not None:
        try:
            await _redis.set(key, blob, ex=L1_TTL_SECONDS, nx=True)
        except Exception:
            pass

    # L2 — atomic write
    p = L2_ROOT / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, p)
```

**Tests**: hit/miss, L1 only, L2 only, both, corrupt JSON recovers, redis-down falls through to L2.

---

## Step 5: Prompt Renderer + 4 Templates

```python
# backend/src/codebase_kb/prompts/render.py
from __future__ import annotations
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TPL_DIR = Path(__file__).parent
_env = Environment(
    loader=FileSystemLoader(str(_TPL_DIR)),
    autoescape=select_autoescape(disabled_extensions=("md",), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

def render_prompt(name: str, **vars) -> str:
    """Load `name.md` and render with `vars`."""
    return _env.get_template(f"{name}.md").render(**vars)
```

Then create the 4 templates. They follow the structure sketched in `post-codegraph-pipeline-plan.md`:

- `prompts/identify.md` — Communities + top-K modules by PageRank → YAML list of `{name, description, anchor_modules}`.
- `prompts/analyze.md` — Abstraction list + projected edge list → YAML `{summary, relationships: [...]}`.
- `prompts/order.md` — Topo-ordered chapter list → 2-3 sentence rationale prose.
- `prompts/write_chapter.md` — One chapter: motivation, key excerpts, sequence diagram, takeaways, next link.

**Important preamble** (include in every template):
> Symbols and file paths in the input below are **guaranteed to exist** in the codebase. Reference them by exact name. Do not invent symbols or file paths.

**Tests** (cheap): `render_prompt("identify.md", top_k=10, communities=[], ...)` returns a non-empty string; missing var raises; unicode passes through.

---

## Step 6: CodeGraph Deserializer + Slicing

**Add to `codeintel/graph.py`**:
```python
import networkx as nx
from networkx.readwrite import json_graph
from typing import Any

@classmethod
def from_payload(cls, payload: dict) -> "CodeGraph":
    """Rehydrate a CodeGraph from `nx.node_link_data` output (or our
    `_meta` wrapper around it)."""
    cg = cls()
    data = payload.get("graph", payload)  # tolerate either
    cg.g = json_graph.node_link_graph(data, edges="edges")
    return cg

def meta(self) -> dict:
    """Lightweight metrics to attach to the serialized payload.
    Computed on demand (cheap; < 50ms for 1k-node graphs)."""
    return {
        "pagerank": nx.pagerank(self.g) if self.g.number_of_nodes() else {},
        "communities": [list(c) for c in self.communities()],
        "node_count": self.g.number_of_nodes(),
        "edge_count": self.g.number_of_edges(),
    }
```

**`codeintel/slicing.py`** (the chapter-prompt builder):
```python
from __future__ import annotations
from typing import Iterable
from ..extract.graph import CodeGraph
from ..utils.tokens import estimate_tokens, truncate_to_tokens

def build_chapter_prompt(abstraction: dict,
                         code_graph: CodeGraph,
                         files_by_path: dict[str, str],
                         *,
                         token_budget: int = 10_000) -> str:
    """Build the `## Relevant Code` block for a chapter.

    Strategy:
    1. sliced_context() -> a list of file paths, ranked by PageRank within
       the anchor's k-hop neighborhood, capped to top-N.
    2. Concatenate file contents until the token budget is hit.
    3. The last included file may be truncated; the prompt footer tells the
       LLM this is intentional.
    """
    paths = code_graph.sliced_context(
        abstraction["anchor_node_ids"], radius=2, max_nodes=50,
    )
    pieces: list[str] = []
    used = 0
    for p in paths:
        content = files_by_path.get(p, "")
        if not content:
            continue
        cost = estimate_tokens(content)
        if used + cost <= token_budget:
            pieces.append(f"# {p}\n```\n{content}\n```")
            used += cost
            continue
        remaining = token_budget - used
        if remaining < 200:
            break
        truncated = truncate_to_tokens(content, remaining)
        pieces.append(f"# {p} (truncated)\n```\n{truncated}\n```")
        used += remaining
        break
    return "\n\n".join(pieces) or "(no relevant files found)"
```

**Tests**: assert the returned string's `estimate_tokens` ≤ `token_budget + 200`; assert anchor files are present; assert unrelated files are absent (build a fixture with two distant files).

---

## Step 7: Output Helpers

`output/writer.py`:
```python
from __future__ import annotations
from pathlib import Path
import os, tempfile

def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
```

`output/zip.py`:
```python
import os, zipfile
from pathlib import Path

def zip_directory(src_dir: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_dir):
            for f in sorted(files):  # deterministic
                full = Path(root) / f
                arc = full.relative_to(src_dir).as_posix()
                zf.write(full, arc)
```

**Tests**: write 3 files, zip, unzip into a new dir, `diff -r` → empty.

---

## Step 8: `graph/state.py`

```python
# backend/src/codebase_kb/graph/state.py
from __future__ import annotations
from typing import TypedDict, Optional, List, Dict, Any
import operator
from typing_extensions import Annotated

class KnowledgeBuilderState(TypedDict, total=False):
    # --- inputs (set once at run start) ---
    run_id: str
    project_id: str
    org_id: str
    repo_url: Optional[str]
    local_dir: Optional[str]                  # alternative to repo_url (CLI/dev)
    project_name: str
    github_token: Optional[str]
    output_dir: str
    include_patterns: List[str]
    exclude_patterns: List[str]
    max_file_size: int
    language: str
    max_abstractions: int
    use_cache: bool
    use_llm_order_rationale: bool             # optional cost/latency toggle
    provider: str
    model: str

    # --- intermediate ---
    files: List[Dict[str, str]]               # [{"path": ..., "content": ...}]
    code_graph: Dict[str, Any]                # {graph: {nodes, edges}, meta: {...}}
    abstractions: List[Dict[str, Any]]
    relationships: List[Dict[str, Any]]
    summary: str                              # prose from analyze_relationships
    chapter_order: List[int]
    rationale: str                            # optional from order_chapters

    # --- outputs (chapters uses a reducer so Send workers merge cleanly) ---
    chapters: Annotated[List[Dict[str, Any]], operator.add]

    final_output_dir: str
    artifacts: List[Dict[str, Any]]           # [{kind, path, size_bytes, sha256}]
    token_usage: Annotated[Dict[str, int], operator.add]   # rolled up per node
```

`total=False` is the right call: nodes add fields incrementally.

---

## Step 9: The 5 Remaining Nodes

These follow the structure exactly from `post-codegraph-pipeline-plan.md` (you have it open already). I'll summarize the per-node contract and the verification approach so you can build them in order.

### Node 3 — `identify_abstractions.py`

```python
def identify_abstractions_node(state: dict) -> dict:
    # 1. (optional) cache lookup keyed by (model, code_graph_hash, max_abstractions, language)
    # 2. CodeGraph.from_payload(state["code_graph"]) — rehydrate
    # 3. Build a "candidate view" from PageRank top-K modules + communities:
    #      top_modules:  [{path, pagerank, top_functions, top_classes}, ...]
    #      communities:  [{pagerank_sum, top_modules: [...]}, ...]
    # 4. render_prompt("identify.md", top_k=..., communities=..., top_modules=..., max_abstractions=..., language=...)
    # 5. provider.complete_async(prompt, temperature=0.2, max_tokens=2048)
    # 6. extract_yaml(response) → list of {name, description, anchor_modules}
    # 7. Validate: each anchor_modules path must exist in state["files"]; drop unknown.
    # 8. Cap at max_abstractions; warn if < 5.
    # 9. (optional) cache_store
    # 10. Return {"abstractions": [...], "token_usage": {...}}
```

**Verification**: feed a fixture state with a 30-module toy graph and a fake LLM returning canned YAML; assert the output list length is in [5, max_abstractions], every `anchor_modules` is in the fixture's `files`, and the cache hits on the second call.

### Node 4 — `analyze_relationships.py`

```python
def analyze_relationships_node(state: dict) -> dict:
    # 1. cache lookup
    # 2. Project code-graph edges onto abstraction level:
    #      for each edge (u, v) in CodeGraph, find which abstractions u, v belong to
    #      (via anchor_node_ids + module-level file_index mapping).
    #      Aggregate (from_abs, to_abs, kind) -> count.
    # 3. render_prompt("analyze.md", abstractions=..., edges=...)
    # 4. provider.complete_async(...)
    # 5. Parse {summary, relationships: [{from, to, label, kind}]}.
    # 6. Validate: every from/to in abs_names; drop self-loops; truncate label to 60.
    # 7. Connectivity check: every abstraction must appear in at least one edge;
    #    if not, add a self-edge so the Mermaid diagram is connected.
    # 8. cache_store; return {"relationships": [...], "summary": "...", "token_usage": {...}}
```

**Verification**: unit test the projection function on a 5-edge fixture; assert the LLM output is filtered correctly (unknown endpoint, self-loop, label truncation).

### Node 5 — `order_chapters.py`

```python
def order_chapters_node(state: dict) -> dict:
    # 1. cache lookup
    # 2. _topo_order(code_graph, abstractions):
    #      - subgraph of anchor_ids + their transitive ancestors
    #      - cycle-break: repeat find_cycle(); remove lowest-PageRank back-edge (max 1000 iters)
    #      - topological_sort
    #      - map back to abstraction indices; append orphans at the end
    # 3. If use_llm_order_rationale: render_prompt("order.md"), complete_async
    # 4. cache_store; return {"chapter_order": [...], "rationale": "...", "token_usage": {...}}
```

**Verification**: unit test the topo order on (a) a DAG, (b) a cycle, (c) an orphan; assert order respects dependencies, all 3 cases include all abstractions.

### Node 6 — `write_chapter_single.py` (the Send target)

```python
async def write_chapter_single(payload: dict) -> dict:
    # payload keys: abstraction_index, abstraction, code_graph, files_by_path,
    #               relationships, output_dir, run_id, org_id, language,
    #               use_cache, provider, model
    #
    # 1. cache lookup (key: chapter:<idx>)
    # 2. CodeGraph.from_payload(payload["code_graph"])
    # 3. build_chapter_prompt(abstraction, g, files_by_path, token_budget=10_000)
    # 4. Compute neighbors of this abstraction for the sequence diagram
    # 5. render_prompt("write_chapter.md", abstraction, chapter_context, neighbors, ...)
    # 6. provider.complete_async(..., temperature=0.3, max_tokens=4096)
    # 7. Extract chapter markdown (find first "# " heading)
    # 8. Inject the programmatically-built sequence diagram (replace or append)
    # 9. write_text(<output_dir>/<idx + 1>_<slug>.md, markdown)
    # 10. cache_store; return {"chapters": [{"index": idx, "name": ..., "markdown": ..., "path": str}]}
```

**Retry + fallback**:
- Wrap steps 6–8 in `for attempt in range(MAX_RETRIES + 1)`. On `ValueError` (no `#` heading) or `MermaidGenError`, retry.
- After all retries fail, write a **skeleton chapter** with the abstraction's description, the list of anchor files, and the error message — the run still succeeds.

**Verification**: integration test with a real LLM against a 3-file toy repo; assert the chapter file exists, starts with `# <name>`, contains a `sequenceDiagram` block, and the "Next:" link is to the right chapter (in a 2-chapter test).

### Node 7 — `combine_tutorial.py`

```python
def combine_tutorial_node(state: dict) -> dict:
    chapters = sorted(state["chapters"], key=lambda c: c["index"])
    abstractions = state["abstractions"]
    relationships = state.get("relationships", [])
    output_dir = Path(state["output_dir"])
    tutorial_dir = output_dir / state.get("project_name", "tutorial")
    tutorial_dir.mkdir(parents=True, exist_ok=True)

    # 1. Build overview Mermaid (try/except → log + continue)
    # 2. Build cycles Mermaid (capped at 5 cycles from CodeGraph)
    # 3. Build index.md and write
    # 4. Renumber chapters to 01_..., 02_..., ...  (rewrite internal Next: links)
    # 5. zip_directory(tutorial_dir, output_dir / "<name>.zip")
    # 6. Return {"final_output_dir": str(tutorial_dir),
    #           "artifacts": [{"kind": "index"|"chapter"|"zip", "path", "size_bytes", "sha256"}]}
```

**Verification**: unit test with a fixture state containing 3 chapters and 2 relationships; assert `index.md` contains the overview diagram, the TOC links to `01_...md`/`02_...md`/`03_...md`, the zip exists and is non-empty.

---

## Step 10: Graph Wiring (`graph/graph.py`)

```python
from __future__ import annotations
from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send

from .state import KnowledgeBuilderState
from .nodes.fetch_repo import fetch_repo_node
from .nodes.build_code_graph import build_code_graph_node
from .nodes.identify_abstractions import identify_abstractions_node
from .nodes.analyze_relationships import analyze_relationships_node
from .nodes.order_chapters import order_chapters_node
from .nodes.write_chapters import write_chapter_single
from .nodes.combine_tutorial import combine_tutorial_node


def route_to_chapter_writers(state: dict) -> list[Send]:
    files_by_path = {f["path"]: f["content"] for f in state["files"]}
    out: list[Send] = []
    for idx in state["chapter_order"]:
        out.append(Send("write_chapter_single", {
            "abstraction_index": idx,
            "abstraction": state["abstractions"][idx],
            "code_graph": state["code_graph"],
            "files_by_path": files_by_path,
            "relationships": state.get("relationships", []),
            "output_dir": state["output_dir"],
            "run_id": state["run_id"],
            "org_id": state["org_id"],
            "language": state.get("language", "english"),
            "use_cache": state.get("use_cache", True),
            "provider": state.get("provider"),
            "model": state.get("model"),
        }))
    return out


def build_graph():
    g = StateGraph(KnowledgeBuilderState)
    g.add_node("fetch_repo",            fetch_repo_node)
    g.add_node("build_code_graph",      build_code_graph_node)
    g.add_node("identify_abstractions", identify_abstractions_node)
    g.add_node("analyze_relationships", analyze_relationships_node)
    g.add_node("order_chapters",        order_chapters_node)
    g.add_node("write_chapter_single",  write_chapter_single)
    g.add_node("combine_tutorial",      combine_tutorial_node)

    g.add_edge(START,                   "fetch_repo")
    g.add_edge("fetch_repo",            "build_code_graph")
    g.add_edge("build_code_graph",      "identify_abstractions")
    g.add_edge("identify_abstractions", "analyze_relationships")
    g.add_edge("analyze_relationships", "order_chapters")
    g.add_conditional_edges("order_chapters", route_to_chapter_writers, ["write_chapter_single"])
    g.add_edge("write_chapter_single",  "combine_tutorial")
    g.add_edge("combine_tutorial",      END)

    # No checkpointer in this phase — runs are short enough to re-run from scratch.
    return g.compile()
```

**One important compatibility fix**: your current `build_code_graph_node.py` returns `{"code_graph": nx.node_link_data(cg.g)}`. With the new `CodeGraph.from_payload`, change the node to also include `_meta` so cache keys can be stable:

```python
# in graph/nodes/build_code_graph.py
from src.codebase_kb.extract.graph import CodeGraph
from src.codebase_kb.utils.hashing import short_hash
import networkx as nx

def build_code_graph_node(state: dict) -> dict:
    cg = CodeGraph()
    for f in state["files"]:
        if f["path"].endswith(".py"):
            nodes, edges = parse_python_file(f["path"], f["content"])
            cg.add_nodes(nodes)
            cg.add_edges(edges)
    payload = {
        "graph": nx.node_link_data(cg.g),
        "meta":  cg.meta(),
    }
    payload["_meta"] = {"graph_hash": short_hash(payload["meta"])}
    return {"code_graph": payload}
```

---

## Step 11: CLI Entry Point

```python
# backend/src/codebase_kb/__main__.py
"""CLI smoke test: run the full pipeline on a local directory or a GitHub URL.

Usage:
    python -m codebase_kb --local-dir ../Q-Learning-From-Scratch --project-name Q-Learning
    python -m codebase_kb --repo-url https://github.com/owner/repo --github-token <token>
"""
import argparse, asyncio, json, sys
from pathlib import Path
from uuid import uuid4

from .graph.graph import build_graph
from .graph.state import KnowledgeBuilderState

def _initial_state(args) -> KnowledgeBuilderState:
    return KnowledgeBuilderState(
        run_id=str(uuid4()),
        project_id="cli",
        org_id="cli",
        repo_url=args.repo_url,
        local_dir=args.local_dir,
        project_name=args.project_name,
        github_token=args.github_token,
        output_dir=str(Path(args.output).resolve()),
        include_patterns=["*.py"],
        exclude_patterns=["venv/*", ".venv/*", "build/*", "dist/*", "__pycache__/*"],
        max_file_size=100_000,
        language=args.language,
        max_abstractions=args.max_abstractions,
        use_cache=not args.no_cache,
        use_llm_order_rationale=args.with_rationale,
        provider=args.provider,
        model=args.model,
    )

async def _amain(args):
    state = _initial_state(args)
    graph = build_graph()
    print(f"[run {state['run_id']}] invoking graph...", file=sys.stderr)
    final = await graph.ainvoke(state)
    print(json.dumps({
        "final_output_dir": final.get("final_output_dir"),
        "abstraction_count": len(final.get("abstractions", [])),
        "chapter_count": len(final.get("chapters", [])),
        "artifacts": final.get("artifacts", []),
    }, indent=2))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo-url", default=None)
    p.add_argument("--local-dir", default=None)
    p.add_argument("--github-token", default=None)
    p.add_argument("--project-name", required=True)
    p.add_argument("--output", default="./out")
    p.add_argument("--language", default="english")
    p.add_argument("--max-abstractions", type=int, default=8)
    p.add_argument("--provider", default="gemini")
    p.add_argument("--model", default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--with-rationale", action="store_true")
    args = p.parse_args()
    asyncio.run(_amain(args))

if __name__ == "__main__":
    main()
```

**Note**: for `local_dir` mode you'll need a tiny `crawler/local.py` (~30 lines: walk the dir, read text files, apply the same binary/size cap, return `List[FileEntry]`). Use the `crawler/models/models.py` `FileEntry` you already have.

---

## Step 12: End-to-End Smoke Test on `Q-Learning-From-Scratch/`

You already have this fixture in your repo root. Run:

```bash
cd backend
pip install -r ../requirements.txt  # if not already
export GEMINI_API_KEY=...           # or whichever provider
python -m codebase_kb \
  --local-dir ../Q-Learning-From-Scratch \
  --project-name Q-Learning \
  --provider gemini \
  --max-abstractions 5 \
  --output ../out
```

Expected: `out/Q-Learning/index.md` opens in any Mermaid viewer, shows 5 chapters, each with a sequence diagram, and the overview diagram matches the file's import graph.

If something is wrong, work backward:
- No `index.md`? → check `combine_tutorial` ran (last node).
- `index.md` has no Mermaid? → check the `relationships` list is non-empty; the LLM may have returned `kind` values outside the whitelist.
- Chapters exist but lack `sequenceDiagram`? → `build_chapter_sequence` raised; check the warning log.
- One chapter is a skeleton? → that chapter's LLM call failed 3×; check the chapter's `anchor_node_ids` map to real files.

---

## Build Schedule (Recommended)

| # | File(s) | Est. | Verify with |
|---|---|---|---|
| 1 | `output/mermaid.py` (rewrite) | 1h | `tests/unit/test_mermaid.py` — adversarial inputs |
| 2 | `utils/{yaml_parse,tokens,hashing}.py` | 30m | `tests/unit/test_utils.py` |
| 3 | `llm/{base,gemini,anthropic,openai_compat,router}.py` | 2h | Fake provider unit tests + a 1-line "hello world" end-to-end |
| 4 | `cache.py` | 1h | L1-only, L2-only, both, corrupt-JSON-recovery tests |
| 5 | `prompts/render.py` + 4 `.md` templates | 2h | `render_prompt(...)` returns string; manual eyeball each |
| 6 | `codeintel/graph.py::from_payload` + `meta()` | 30m | Round-trip: build → serialize → deserialize → same node count |
| 7 | `codeintel/slicing.py` | 1h | Token-budget assertion; anchor files present; unrelated absent |
| 8 | `output/{writer,zip}.py` | 30m | Atomic write + zip round-trip |
| 9 | `graph/state.py` | 30m | Importable; annotations correct (run mypy if you have it) |
| 10 | `graph/nodes/build_code_graph.py` (fix to include `_meta`) | 15m | Existing tests still pass |
| 11 | `graph/nodes/identify_abstractions.py` | 2h | Fake LLM returns valid + invalid YAML; both paths work |
| 12 | `graph/nodes/analyze_relationships.py` | 2h | Edge projection + connectivity fallback tests |
| 13 | `graph/nodes/order_chapters.py` | 1h | DAG / cycle / orphan tests |
| 14 | `graph/nodes/write_chapters.py` (with retry + skeleton) | 3h | Real LLM against a 3-file fixture; assert 2 valid + 1 skeleton on forced failure |
| 15 | `graph/nodes/combine_tutorial.py` | 1h | Fixtures with 3 chapters → inspect `index.md` + zip |
| 16 | `graph/graph.py` (wiring) | 30m | `build_graph()` compiles; `graph.ainvoke` runs to completion on the Q-Learning fixture |
| 17 | `__main__.py` + tiny `crawler/local.py` | 30m | CLI runs end-to-end on Q-Learning |
| 18 | End-to-end on Q-Learning-From-Scratch (real LLM) | 1h | Eyeball `out/Q-Learning/index.md`; assert every `file:line` excerpt exists |

**Total**: ~20–24 focused hours.

---

## Integration Checklist (Before End-to-End Run)

Confirm the following before invoking the graph:

- [ ] `output/mermaid.py` exports `build_overview_diagram`, `build_chapter_sequence`, `build_cycles_diagram`, `MermaidGenError` (the API used by `analyze_relationships`, `write_chapter_single`, `combine_tutorial`).
- [ ] `codeintel/graph.py::CodeGraph.from_payload()` rehydrates a `nx.node_link_data` dict.
- [ ] `build_code_graph_node` returns `{"code_graph": {"graph": ..., "meta": ..., "_meta": {"graph_hash": ...}}}` — i.e. it includes the hash used by cache keys.
- [ ] `llm/router.get_provider(...)` returns a provider with a truly-async `complete_async` (wrap sync SDKs with `asyncio.to_thread`).
- [ ] `cache.cache_lookup` / `cache.cache_store` use a stable `org_id` namespace (default `"_default"` until per-org auth exists).
- [ ] `prompts/render.render_prompt` exists and the 4 `.md` files are in the same directory.
- [ ] `utils/yaml_parse.extract_yaml` returns `None` for non-fenced text (don't crash).
- [ ] `graph/state.py` declares `chapters: Annotated[List[...], operator.add]` so the `Send` reducer merges parallel writes.
- [ ] `graph/graph.py::route_to_chapter_writers` returns a list of `Send("write_chapter_single", payload)` — not a single call.

---

## Verification (Definition of Done)

**Unit** (run with `pytest backend/tests/unit -q`):
- All 4 new utils pass.
- `mermaid` produces valid output on 5 adversarial inputs; `MermaidGenError` raised on bad header.
- `cache` L1+L2 hit/miss/corrupt recovery all pass.
- `codeintel.from_payload` round-trips on a 100-node graph (node count, edge count, attributes preserved).
- `slicing.build_chapter_prompt` stays within `token_budget + 200` on a 10-file fixture.
- `identify_abstractions` filters out unknown anchors; caps at `max_abstractions`; cache hits on second call.
- `analyze_relationships` projects 5 edges → 2 collapsed abstract edges; drops unknown endpoints; adds self-edge for uncovered.
- `order_chapters` produces a valid permutation for DAG / cycle / orphan cases.
- `write_chapter_single` retries 2× on bad LLM output, succeeds on the 3rd; writes a skeleton on total failure.
- `combine_tutorial` writes `index.md` with `flowchart TD`, renumbers chapters 01..N, produces a non-empty zip.

**Integration** (`pytest backend/tests/integration -q`):
- Full graph run on `tests/fixtures/tiny_repo/` (create if missing — a 3-file Python toy: `service.py`, `store.py`, `api.py`) with a **fake LLM** fixture that returns canned responses.
- Resulting `index.md` contains all 3 chapter names; each chapter file contains a `sequenceDiagram` block; zip is non-empty.

**Manual** (real LLM):
```bash
cd backend
export GEMINI_API_KEY=...
python -m codebase_kb \
  --local-dir ../Q-Learning-From-Scratch \
  --project-name Q-Learning \
  --provider gemini --max-abstractions 5
open ../out/Q-Learning/index.md     # or open in your Markdown viewer
```
- [ ] Overview Mermaid renders.
- [ ] 5 chapter files exist and link to each other.
- [ ] Every code excerpt's `file:line` is real (`grep -n` each).
- [ ] Each chapter has a `sequenceDiagram` that renders.
- [ ] Total wall-clock ≤ 2 min; total token usage ≤ 100K.

**Negative paths to verify**:
- LLM returns `null` for YAML → node retries 3×; final run status is `failed` with a clear error in the run record.
- LLM returns a path not in the repo for an anchor → that abstraction is dropped silently; rest of the run succeeds.
- Graph has 0 internal modules → `identify_abstractions` returns ≤ 5 placeholders; chapters are skeletons.
- `output_dir` is not writable → `combine_tutorial` fails with `PermissionError`; run status is `failed`; partial chapters are recoverable from disk.

---

## Open Questions (Defaults Suggested)

1. **LLM call in `order_chapters`**: default **off** (the topo sort is the right answer; LLM rationale is a nice-to-have). Enable with `--with-rationale`.
2. **Cycle diagram in `combine_tutorial`**: include only if `len(cycles) ≥ 3` (otherwise too noisy).
3. **Skeleton chapter on failure**: default **yes** — better to ship 4 good + 1 stub than to fail the run.
4. **Per-chapter cache TTL**: **forever** until the graph hash changes.
5. **Diagram style**: `flowchart TD` (top-down) for ≤ 15 nodes; `flowchart LR` (left-right) otherwise.

---

## What's Explicitly Out of Scope (For Future Phases)

- DB models + Alembic migrations
- GitHub OAuth + JWT
- Arq workers + Redis pub/sub for progress
- FastAPI routers + SSE endpoint
- Next.js frontend
- Per-org LLM key table (router currently uses env)
- S3 / MinIO artifact upload
- Per-org quotas, billing, webhooks
- Observability (structlog, Prometheus, OpenTelemetry)
- Docker / K8s / CI/CD

These are described in `detailed-plan-codebase-knowledge-builder.md` and remain valid; they're sequenced **after** the narrative layer produces a working tutorial end-to-end.

---

## Summary

You've built the code-intel layer; now build the **narrative layer** that turns a code graph into a Markdown tutorial:

1. Fix `output/mermaid.py` to the new API.
2. Add 3 small utils (yaml, tokens, hashing).
3. Add 4 LLM providers + a router.
4. Add a two-tier cache.
5. Add 4 prompt templates + a renderer.
6. Add `CodeGraph.from_payload`, `meta()`, and a `slicing.py`.
7. Add 5 graph nodes (identify, analyze, order, write_chapter, combine).
8. Wire the graph with `Send` fan-out.
9. Add a CLI entry point.
10. End-to-end test on `Q-Learning-From-Scratch/`.

The single biggest design choice: every LLM call is **bounded** (max 10K input tokens) and **grounded** (every file path, every symbol is real — verified by the graph). The LLM is the *narrator*, never the *inventor*.

After this phase, you'll have a working CLI that produces a real tutorial for any Python repo. Auth, API, workers, and frontend are straightforward bolt-ons after that.
