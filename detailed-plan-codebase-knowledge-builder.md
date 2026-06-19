# Automated Codebase Knowledge Builder — Detailed Plan (SaaS Edition, Teacher Edition)

## What You Are Building (Pivot)

A **multi-tenant SaaS web application** that ingests a code repository (Git URL or uploaded tarball), reverse-engineers its architecture, and emits a structured Markdown tutorial (`index.md` + numbered chapter files) with auto-generated Mermaid diagrams. Users sign in with GitHub OAuth, point the tool at a repo, watch progress live in the browser, and browse the resulting tutorial or download it as a zip.

**Why the pivot**: a CLI is a developer tool; a SaaS is a product. The CLI required every user to install Python, configure env vars, and babysit a long-running process. The SaaS lowers the activation cost to "click and wait" — and turns a one-off tool into a recurring-revenue business.

**Why it matters (still)**: onboarding to large, undocumented repos is one of the most expensive bottlenecks in software engineering. Existing tools fail differently — wikis go stale, docstring generators are too granular, semantic search (Bloop, Sourcegraph) requires the developer to already know what to look for. This product builds the mental map automatically.

**Reference architecture**: [PocketFlow-Tutorial-Codebase-Knowledge](https://the-pocket.github.io/PocketFlow-Tutorial-Codebase-Knowledge/) — a 6-node linear pipeline (Fetch → Identify → Analyze → Order → Write (BatchNode) → Combine), extended to **7 nodes** with a new `build_code_graph` stage, re-implemented in **LangGraph**, running as an asynchronous background job per request.

**Working directory**: `C:\Users\HP\OneDrive\Desktop\temp` (the project will be scaffolded as `codebase-kb/` inside this directory).

---

## The Big Picture: Pipeline Mental Model

Think of the system as **two layers**: a **synchronous HTTP layer** (FastAPI handles "start a run", "show me progress", "download result") and an **asynchronous pipeline layer** (LangGraph executes the 7-node assembly line in a worker, publishing progress to Redis pub/sub which the frontend consumes over Server-Sent Events).

The pipeline itself is the same assembly line as before, with one new station bolted on:

```
START
  └─> fetch_repo                          (worker: clone or upload, dedupe, size cap)
        └─> build_code_graph              (Python: ast → NetworkX DiGraph)
              └─> identify_abstractions   (graph-driven + LLM narrative)
                    └─> analyze_relationships  (hybrid: AST edges are ground truth, LLM adds labels)
                          └─> order_chapters   (graph topo sort + optional LLM refinement)
                                └─> [Send] -> write_chapter_single (×N, parallel, sliced ctx)
                                      └─> combine_tutorial (programmatic Mermaid + zip + upload)
                                            └─> END
```

**The 7 nodes, in plain English:**

1. **fetch_repo** — Go grab the code. Either clone via GitHub API (using the user's OAuth token) or extract an uploaded tarball. Skip binaries, skip giant files, respect include/exclude patterns, dedupe.
2. **build_code_graph** *(NEW)* — Parse every Python file with `ast`, extract imports/functions/classes/calls/inheritance/decorators, build a directed graph in `networkx`. Compute PageRank, communities, and a topological sort. **This is the layer that solves the token problem.**
3. **identify_abstractions** — Show the LLM only the top-K PageRank candidates (not the whole repo) and ask: "What are the 5–15 core concepts?" Output: a list of `{name, description, anchor_node_ids, file_indices}`.
4. **analyze_relationships** — The graph already contains import/call/inherit edges. Ask the LLM to label them and optionally add semantic edges that aren't visible in the AST (e.g. "the Auth module *orchestrates* the User module"). Output: directed edges with `kind` ∈ `{import, call, inherit, semantic}`.
5. **order_chapters** — The graph's topological sort gives a valid teaching order for free (after breaking cycles). The LLM call is optional — it can refine the order based on pedagogical sense.
6. **write_chapter_single** (×N, parallel) — For each concept, slice the code graph to the k-hop neighborhood of the concept's anchor symbols, feed ONLY that slice (plus the abstraction description) to the LLM, and ask for a Markdown chapter with code excerpts and a Mermaid sequence diagram.
7. **combine_tutorial** — Programmatically stitch everything together: build the overview Mermaid `flowchart TD` from the validated graph + labeled edges, generate the `index.md` table of contents, zip everything, upload to S3-compatible storage. This step is **not** LLM-driven — diagrams are built from validated data.

---

## Core Concept 1: The Token Problem (Why AST + NetworkX)

A naïve implementation feeds the whole repo to the LLM at each step. This works on toy repos (~10 files). It breaks on real-world repos:

- **Cost**: a 1,000-file Python codebase is ~500K LOC → ~50M tokens of source → ~$500 and 2 hours per single `identify_abstractions` call.
- **Hallucination**: when context is large, the LLM invents symbols, misattributes files, generates plausible-but-wrong relationship edges. The diagrams then lie, and the tutorial is worse than nothing.
- **Accuracy degradation**: even with 1M-token context windows, accuracy drops sharply past ~200K tokens (the lost-in-the-middle effect — Claude and Gemini both lose track of facts buried in long contexts).

**The two tools that make this tractable:**

| Tool | Role | What it gives us |
|---|---|---|
| `ast` (stdlib) | Parse Python into a syntax tree | Imports, function/class signatures, call sites, inheritance, decorators — *ground truth*, no guessing |
| `networkx` | Build and analyze a code graph | PageRank (core abstractions), community detection (chapter groups), topological sort (teaching order), k-hop neighborhoods (sliced context) |

**The graph we build** (per repo):

- **Nodes**: one per `Module`, per top-level `FunctionDef`/`AsyncFunctionDef`, per `ClassDef`. Each node carries `file`, `lineno`, `end_lineno`, `kind`, `name`, `signature`, `docstring`.
- **Edges (directed)**:
  - `imports`: module A → module B (from `Import`/`ImportFrom`)
  - `calls`: function A → function B (resolved via `ast.Call.func` + symbol table)
  - `inherits`: class A → class B (from `ClassDef.bases`)
  - `contains`: module A → function/class A.X
  - `decorates`: function A → function B (from `Decorator` nodes)

**What we compute for free with `networkx`:**

| Metric | Algorithm | Token-relevance |
|---|---|---|
| Top-K core abstractions | `nx.pagerank(G)` | LLM identifies abstractions from a short, ranked list — not the whole repo |
| Chapter grouping | `nx.community.louvain_communities(G)` | Each community becomes a chapter group; intra-group context only |
| Teaching order | `nx.topological_sort(DAG)` after cycle-break | No LLM call needed for ordering — the graph gives it for free |
| Per-chapter sliced context | `nx.ego_graph(G, anchor, radius=k)` | When writing chapter N, feed only anchor's k-hop neighborhood (filtered to top-N by PageRank inside) |
| Cycle detection | `nx.find_cycle()` / `nx.simple_cycles()` | Highlights architectural smells; becomes a chapter section |
| Code smells | `in_degree` / `out_degree` / fan-in / fan-out | God modules, dead code, hub files — surfaced in the tutorial |

**Token budget math** (illustrative for a 1000-file Python repo):

| Approach | Tokens per LLM call | Total cost | Time | Quality |
|---|---|---|---|---|
| Whole-repo dump (naïve) | ~500K × 15 calls | ~$15/run | 90 min | Hallucinates, diagrams lie |
| AST summary only | ~50K × 15 calls | ~$1.50/run | 9 min | Better, but lacks context for prose |
| **Graph-sliced (our approach)** | ~10K × 15 calls | **~$0.30/run** | **~3 min** | **Best — every excerpt is real, every edge is grounded** |

**Quality wins**: the LLM can no longer invent a relationship between files that don't import each other, or quote a function that doesn't exist. The graph is the source of truth. Prompts tell the model: *"the symbols and edges in the input below are guaranteed to exist in the codebase; use them."* This both reduces hallucination and shortens the prompt.

---

## Core Concept 2: LangGraph StateGraph + Send (unchanged)

A `StateGraph` is a workflow engine where data flows through nodes via a shared `TypedDict`. The new `code_graph` field is just another field in the state — the next node reads it without rebuilding.

**Key trick: `Send`**. When `order_chapters` finishes, we don't know in advance how many chapters to write — the LLM (and the graph) decide. The `Send` primitive lets one node dynamically spawn N parallel workers, each with its own mini-payload. The `chapters` field uses a **reducer** (`Annotated[List, operator.add]`) so all parallel writes merge cleanly into one list.

**Why parallel chapter writing matters**: A repo with 10 concepts means 10 LLM calls. Sequential = 10× latency. Parallel = 1× latency. For a 2-minute-per-call LLM, this saves ~18 minutes per run. With graph-sliced context (~10K tokens), each call is more like 10–20 seconds, so 10 parallel calls finish in ~20s wall-clock instead of ~3 minutes sequentially.

---

## Directory Layout — Why Each Folder Exists

```
codebase-kb/
├── README.md                          # Quickstart, env vars, design overview
├── docker-compose.yml                 # postgres, redis, minio, api, worker, web
├── .env.example                       # All env vars documented
├── pyproject.toml                     # Backend deps
├── package.json                       # Frontend deps
├── Makefile                           # Common dev commands
│
├── backend/                           # FastAPI + LangGraph + workers
│   ├── src/codebase_kb/
│   │   ├── main.py                    # FastAPI app factory + middleware
│   │   ├── config.py                  # pydantic-settings (env loader)
│   │   ├── deps.py                    # FastAPI dependencies (db, current_user, provider)
│   │   ├── db/                        # SQLAlchemy + Alembic
│   │   ├── auth/                      # GitHub OAuth, JWT, RBAC
│   │   ├── api/v1/                    # REST endpoints (projects, runs, artifacts, billing, webhooks)
│   │   ├── api/ws.py                  # WebSocket endpoint for live progress
│   │   ├── workers/                   # Arq task definitions + progress publisher
│   │   ├── graph/                     # LangGraph pipeline
│   │   │   ├── state.py
│   │   │   ├── graph.py               # build_graph() with Send fan-out
│   │   │   └── nodes/                 # one file per pipeline node
│   │   ├── codeintel/                 # ← NEW: AST + NetworkX
│   │   │   ├── ast_python.py          # ast.parse → structural nodes/edges
│   │   │   ├── graph.py               # NetworkX wrapper, metrics, slicing
│   │   │   ├── slicing.py             # ego_graph, page-rank-filtered context
│   │   │   └── models.py              # CodeNode, CodeEdge dataclasses
│   │   ├── crawler/                   # GitHub API / local / upload
│   │   ├── llm/                       # Pluggable provider abstraction + per-org router
│   │   ├── cache.py                   # Two-tier: Redis (L1) + disk (L2)
│   │   ├── prompts/                   # 4 prompt templates (identify, analyze, order, write_chapter)
│   │   ├── output/                    # Mermaid generator, file writer, zip
│   │   ├── observability/             # structlog, prometheus, opentelemetry
│   │   └── utils/                     # yaml_parse, hashing, tiktoken wrapper
│   └── tests/
│
├── frontend/                          # Next.js 14 + TypeScript + Tailwind + shadcn
│   ├── src/app/                       # App Router pages
│   ├── src/components/                # Mermaid viewer, markdown viewer, run progress, file tree
│   ├── src/lib/                       # api-client, auth, ws
│   └── tests/                         # Playwright e2e
│
├── infra/                             # Dockerfiles, CI, K8s manifests
└── docs/                              # design.md, architecture.md, api.md, runbook.md
```

**Folder-by-folder reasoning:**

- `main.py` — FastAPI app factory: middleware (CORS, logging, rate limit), router includes, exception handlers, startup/shutdown for db pool and redis.
- `config.py` — pydantic-settings reads env vars once at startup; raises on missing required values with an actionable message.
- `deps.py` — FastAPI dependency injection: `get_db`, `get_current_user`, `get_org_for_user`, `get_provider_for_org`. Keeps endpoint handlers thin.
- `db/` — SQLAlchemy 2.0 async models, Alembic migrations. RLS or repository pattern enforces org scoping.
- `auth/` — GitHub OAuth flow (302 to GitHub, callback exchanges code for token, stores encrypted refresh token, sets httpOnly cookies); JWT issuance; RBAC permissions.
- `api/v1/` — One router file per resource (`projects.py`, `runs.py`, etc.). All endpoints return pydantic models, validate input with pydantic, scope by `org_id` from the auth dependency.
- `api/ws.py` — WebSocket endpoint per run for live progress (alternative to SSE).
- `workers/` — Arq task definitions; one task = one run. Publishes progress to a Redis channel per run_id; the SSE endpoint subscribes.
- `graph/` — The pipeline. `state.py` defines the clipboard, `graph.py` wires the 7 nodes, `nodes/` are the workers.
- **`codeintel/`** — **The new core.** `ast_python.py` walks Python AST and emits `CodeNode`/`CodeEdge` records. `graph.py` builds a `networkx.DiGraph` and exposes `core_abstractions()`, `communities()`, `chapter_order_indices()`, `sliced_context()`. `slicing.py` builds the per-chapter LLM prompt with token budgeting. `models.py` defines the dataclasses.
- `crawler/` — Three implementations of one interface (`List[FileEntry]`): `github.py` (uses user's OAuth token), `local.py` (admin/dev), `upload.py` (tarball). All do binary detection and size capping.
- `llm/` — The pluggable LLM layer. `base.py` defines the `LLMProvider` Protocol. Each provider is a thin SDK wrapper. **`router.py` is new** — it resolves the provider per org by reading `api_keys` (BYO = "bring your own key").
- `cache.py` — Two-tier cache: Redis (hot, TTL'd) + disk (cold, per-org). Per-org namespacing for tenant isolation.
- `prompts/` — 4 Markdown prompt templates with `{{var}}` placeholders. Same as before, but identify/write_chapter inputs are now pre-sliced.
- `output/` — `mermaid.py` (programmatic diagram generation with sanitization), `writer.py` (writes to local temp + uploads to S3), `zip.py` (produces downloadable artifact).
- `observability/` — `structlog` JSON logs, `prometheus_client` metrics, `opentelemetry` traces (langfuse optional for LLM traces).
- `utils/` — `yaml_parse.py` (safe extract from ```` ```yaml ```` fences), `hashing.py` (sha256 keys), `tokens.py` (tiktoken wrapper for budget checks).

---

## State Shape — The Shared Clipboard

`backend/src/codebase_kb/graph/state.py`:

```python
class KnowledgeBuilderState(TypedDict, total=False):
    # --- inputs (set once when run starts) ---
    run_id: str
    project_id: str
    org_id: str
    repo_url: Optional[str]
    local_dir: Optional[str]
    project_name: str
    github_token: Optional[str]              # user's OAuth token (in-memory only)
    output_dir: str
    include_patterns: List[str]
    exclude_patterns: List[str]
    max_file_size: int
    language: str
    max_abstractions: int
    use_cache: bool
    provider: str
    model: str

    # --- intermediate ---
    files: List[Dict[str, str]]              # [{"path": ..., "content": "<source + ast summary>"}]
    code_graph: Dict[str, Any]               # ← NEW: serialized NetworkX graph
    abstractions: List[Dict[str, Any]]       # [{name, description, anchor_node_ids, file_indices}]
    relationships: List[Dict[str, Any]]      # [{from, to, label, kind}]
    chapter_order: List[int]

    # --- outputs ---
    chapters: List[Dict[str, Any]]           # [{index, name, markdown}, ...] (reducer-merged)
    final_output_dir: str
    token_usage: Dict[str, int]              # ← NEW: tracked per node for billing/observability
```

**Key idea**: `chapters` is declared `Annotated[List[Dict[str, Any]], operator.add]` so `Send` workers can each return `{"chapters": [single]}` and the framework concatenates automatically.

**`code_graph` is a serialized form** (nodes + edges + precomputed metrics) stored in state so the next node can read metrics (PageRank, communities) without rebuilding the graph. After the run, it's also persisted to the DB as a JSON blob on the `runs` row for debugging.

---

## StateGraph Wiring

`backend/src/codebase_kb/graph/graph.py`:

```python
from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send

def build_graph():
    g = StateGraph(KnowledgeBuilderState)
    g.add_node("fetch_repo",            fetch_repo_node)
    g.add_node("build_code_graph",      build_code_graph_node)     # NEW
    g.add_node("identify_abstractions", identify_abstractions_node)
    g.add_node("analyze_relationships", analyze_relationships_node)
    g.add_node("order_chapters",        order_chapters_node)
    g.add_node("write_chapter_single",  write_chapter_single)      # Send target
    g.add_node("combine_tutorial",      combine_tutorial_node)

    g.add_edge(START,                   "fetch_repo")
    g.add_edge("fetch_repo",            "build_code_graph")        # NEW edge
    g.add_edge("build_code_graph",      "identify_abstractions")
    g.add_edge("identify_abstractions", "analyze_relationships")
    g.add_edge("analyze_relationships", "order_chapters")
    g.add_conditional_edges(
        "order_chapters",
        route_to_chapter_writers,
        ["write_chapter_single"],
    )
    g.add_edge("write_chapter_single",  "combine_tutorial")
    g.add_edge("combine_tutorial",      END)
    return g.compile()
```

**`route_to_chapter_writers(state)`**: for each `idx` in `state["chapter_order"]`, emit `Send("write_chapter_single", WriteChapterInput(abstraction_index=idx, code_graph=state["code_graph"], sliced_context=...))`.

### Node Read/Write Matrix

| Node | Reads | Writes | LLM? |
|---|---|---|---|
| fetch_repo | repo_url, github_token, filters | `files`, `output_dir` | no |
| **build_code_graph** | `files` | `code_graph` | no |
| identify_abstractions | `code_graph` (top-K PageRank), `files` | `abstractions` | yes (short prompt) |
| analyze_relationships | `abstractions`, `code_graph` (edges are ground truth) | `relationships` | yes (labels only) |
| order_chapters | `code_graph` (topo sort), `abstractions` | `chapter_order` | optional (refinement) |
| write_chapter_single | `code_graph` (ego_graph sliced), `abstractions[i]` | `chapters[i]` (append via reducer) | yes (sliced context) |
| combine_tutorial | `chapters`, `abstractions`, `relationships`, `code_graph` | filesystem writes, artifact upload, `final_output_dir` | no |

---

## CodeIntel Layer — The New Core (Deep Dive)

### `codeintel/models.py` — dataclasses

```python
@dataclass
class CodeNode:
    id: str                                # e.g. "fn:src/auth/service.py:login"
    kind: str                              # "module" | "function" | "method" | "class"
    name: str
    file: str
    lineno: int
    end_lineno: int
    signature: str                         # ast.unparse(args)
    docstring: str

@dataclass
class CodeEdge:
    src: str
    dst: str
    kind: str                              # "import" | "call" | "inherits" | "contains" | "decorates"
    label: str = ""                        # filled by analyze_relationships LLM call
```

### `codeintel/ast_python.py` — AST → nodes/edges

```python
def parse_python_file(path: str, source: str) -> Tuple[List[CodeNode], List[CodeEdge]]:
    tree = ast.parse(source, filename=path)
    nodes: list[CodeNode] = []
    edges: list[CodeEdge] = []

    # Module node
    nodes.append(CodeNode(
        id=f"mod:{path}", kind="module", name=path,
        file=path, lineno=1, end_lineno=tree.end_lineno or 1,
        signature="", docstring=ast.get_docstring(tree) or "",
    ))

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                edges.append(CodeEdge(
                    src=f"mod:{path}", dst=f"mod:{alias.name}",
                    kind="import", label=alias.asname or alias.name,
                ))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_id = f"fn:{path}:{stmt.name}"
            nodes.append(CodeNode(
                id=fn_id, kind="function", name=stmt.name, file=path,
                lineno=stmt.lineno, end_lineno=stmt.end_lineno or stmt.lineno,
                signature=ast.unparse(stmt.args),
                docstring=ast.get_docstring(stmt) or "",
            ))
            edges.append(CodeEdge(src=f"mod:{path}", dst=fn_id, kind="contains"))
            _walk_calls(stmt, fn_id, edges)
            for d in stmt.decorator_list:
                edges.append(CodeEdge(src=fn_id, dst=_dotted(d), kind="decorates"))
        elif isinstance(stmt, ast.ClassDef):
            cls_id = f"cls:{path}:{stmt.name}"
            nodes.append(CodeNode(
                id=cls_id, kind="class", name=stmt.name, file=path,
                lineno=stmt.lineno, end_lineno=stmt.end_lineno or stmt.lineno,
                signature=", ".join(ast.unparse(b) for b in stmt.bases),
                docstring=ast.get_docstring(stmt) or "",
            ))
            edges.append(CodeEdge(src=f"mod:{path}", dst=cls_id, kind="contains"))
            for base in stmt.bases:
                edges.append(CodeEdge(src=cls_id, dst=_dotted(base), kind="inherits"))
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_id = f"fn:{path}:{stmt.name}.{item.name}"
                    nodes.append(CodeNode(
                        id=m_id, kind="method", name=f"{stmt.name}.{item.name}", file=path,
                        lineno=item.lineno, end_lineno=item.end_lineno or item.lineno,
                        signature=ast.unparse(item.args),
                        docstring=ast.get_docstring(item) or "",
                    ))
                    edges.append(CodeEdge(src=cls_id, dst=m_id, kind="contains"))
    return nodes, edges
```

`_walk_calls(node, src_id, edges)` recurses through the AST, finds every `ast.Call`, takes the leftmost name of `Call.func`, and emits a `call` edge to it. Names that don't resolve to a known node become candidate dotted names (`json.dumps`, `requests.get`).

**Graceful degradation**: if `ast.parse` raises (syntax error, encoding issue), the file is skipped and a warning is logged. The pipeline never crashes on one bad file.

### `codeintel/graph.py` — NetworkX wrapper

```python
class CodeGraph:
    def __init__(self):
        self.g = nx.DiGraph()

    def add_nodes(self, nodes: list[CodeNode]) -> None:
        for n in nodes:
            self.g.add_node(n.id, **asdict(n))

    def add_edges(self, edges: list[CodeEdge]) -> None:
        for e in edges:
            self.g.add_edge(e.src, e.dst, kind=e.kind, label=e.label)

    def core_abstractions(self, k: int = 15) -> list[tuple[str, float]]:
        """Top-K nodes by PageRank — the architecturally important ones."""
        return sorted(nx.pagerank(self.g).items(), key=lambda x: -x[1])[:k]

    def communities(self) -> list[set[str]]:
        """Louvain communities — natural chapter groupings."""
        return nx.community.louvain_communities(self.g.to_undirected(), seed=42)

    def chapter_order_indices(self, abstraction_ids: list[str]) -> list[int]:
        """Topological sort over the abstraction DAG, breaking cycles by PageRank."""
        sub = self.g.subgraph(abstraction_ids).copy()
        pr = nx.pagerank(sub)
        for _ in range(1000):
            try:
                cyc = nx.find_cycle(sub)
            except nx.NetworkXNoCycle:
                break
            edge = min(cyc, key=lambda e: pr.get(e[0], 0) + pr.get(e[1], 0))
            sub.remove_edge(*edge[:2])
        return [abstraction_ids.index(n) for n in nx.topological_sort(sub)]

    def sliced_context(self, anchor_ids: list[str], radius: int = 2,
                       max_nodes: int = 50) -> list[str]:
        """k-hop neighborhood, filtered to top-N by PageRank. Returns file paths
        whose content should be in the LLM's prompt for this anchor."""
        nodes = set()
        for a in anchor_ids:
            nodes.update(nx.ego_graph(self.g, a, radius=radius).nodes)
        sub = self.g.subgraph(nodes)
        pr = nx.pagerank(sub)
        keep = set(n for n, _ in sorted(pr.items(), key=lambda x: -x[1])[:max_nodes])
        return sorted({self.g.nodes[n]["file"] for n in keep})
```

### `codeintel/slicing.py` — token-budgeted context builder

```python
def build_chapter_prompt(anchor: dict, code_graph: CodeGraph,
                         files_by_path: dict[str, str],
                         token_budget: int = 10_000) -> str:
    """Build a prompt with ONLY the relevant files for this chapter."""
    paths = code_graph.sliced_context(anchor["anchor_node_ids"], radius=2)
    pieces = []
    used = 0
    enc = tiktoken.encoding_for_model("gpt-4")   # approximate
    for p in paths:
        content = files_by_path.get(p, "")
        cost = len(enc.encode(content))
        if used + cost > token_budget:
            content = _truncate_to_tokens(content, token_budget - used, enc)
            pieces.append(f"# {p}\n{content}\n# [truncated — see {p} for full source]")
            break
        pieces.append(f"# {p}\n{content}")
        used += cost
    return "\n\n".join(pieces)
```

This is the **trick that makes the whole approach work**: every prompt to the LLM is bounded by a known token budget, and the content is the *exact set* of files the LLM needs to write about that chapter — no more.

---

## LLM Provider Abstraction — Pluggable Backend, Per-Org Keys

`backend/src/codebase_kb/llm/base.py`:

```python
class LLMProvider(Protocol):
    def complete(self, prompt: str, *, temperature: float = 0.2,
                 max_tokens: int = 4096) -> str: ...
```

**Provider impls** (unchanged from the original plan):
- `GeminiProvider` — `google-genai` SDK
- `AnthropicProvider` — `anthropic` SDK (Claude)
- `OpenAICompatProvider` — `openai` SDK with `base_url` injectable
- `OllamaProvider` — alias to `OpenAICompatProvider` with default base URL

**The new piece — `llm/router.py`:**

```python
def get_provider_for_org(org_id: str) -> LLMProvider:
    row = db.fetch_one(
        "SELECT provider, encrypted_key, model FROM api_keys WHERE org_id = %s ORDER BY last_used_at DESC LIMIT 1",
        (org_id,),
    )
    if not row:
        raise NoAPIKeyError(f"Org {org_id} has no API keys configured")
    decrypted = fernet.decrypt(row.encrypted_key.encode()).decode()
    return _factory(row.provider, model=row.model, api_key=decrypted)
```

**Why BYO keys (Bring Your Own Key)**:
- We never proxy LLM calls — the user's source code never leaves their provider's infrastructure.
- Each org pays their own LLM bill directly; we don't have to handle markup or chargebacks.
- Regulatory win for users with sensitive code (healthcare, finance, defense).
- Provider outages are the user's problem to handle (multi-provider support lets them switch in seconds).

---

## Prompt Strategy — LLM as Narrative Generator

All prompts live in `backend/src/codebase_kb/prompts/*.md` as Markdown templates with `{{var}}` placeholders. Each instructs the model to emit exactly one fenced YAML/Markdown block, parsed by `utils/yaml_parse.py:extract_yaml(response)`.

| Prompt | Output shape | Drives | Input source |
|---|---|---|---|
| `identify.md` | YAML list of `{name, description, file_indices}` (≤ `max_abstractions`) | `abstractions` | **Top-K PageRank candidates only** (not whole repo) |
| `analyze.md` | YAML `{summary, relationships: [{from, to, label, kind}]}` | `relationships` | **Graph edges as ground truth; LLM only adds labels + kind=semantic** |
| `order.md` | YAML ordered list of concept names | `chapter_order` (refined) | Optional — graph topo sort is primary |
| `write_chapter.md` | Markdown tutorial | `chapters[i].markdown` | **Sliced k-hop subgraph context only** |

**The new prompt header** for `identify.md` and `write_chapter.md` says explicitly:

> "The file paths and symbol names below are guaranteed to exist in the codebase. Reference them by their exact name. Do not invent symbols or file paths."

This both reduces hallucination and shortens the prompt. The LLM is told what's real so it doesn't have to guess.

---

## Mermaid Generation — Why It's Programmatic (unchanged)

`build_overview_diagram(abstractions, relationships) -> str` and `build_chapter_sequence(...) -> str` work the same way. The relationship edges now come from the graph (validated) plus optional LLM-supplied semantic labels. The diagrams are therefore **both grounded and labeled**.

```
flowchart TD
    A["Authentication"]
    B["Session Store"]
    A -->|"issues token for"| B
```

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
1. **Node IDs**: `re.sub(r"[^A-Za-z0-9_]", "_", name)[:40]`; prefix `n` if leading digit; numeric suffix on collision.
2. **Labels**: escape `"` → `#quot;`, collapse `\n` → space, truncate to 60 chars + `…`; HTML-escape `<>|&`.
3. **Edge labels**: same rules; non-empty fallback `"uses"`.
4. **Reserved-keyword guard**: `end|subgraph|graph|class` → append `_node`.
5. **Validation**: regex check on first non-empty line; raise `MermaidGenError` and write the chapter without the diagram on failure.

**Why "don't abort"**: Mermaid syntax is finicky. If one chapter's diagram fails validation, the user still gets N-1 valid chapters. Better to degrade gracefully than to lose the whole tutorial.

---

## Caching Layer — Two-Tier, Per-Org

**L1: Redis** — TTL'd (24h) hot path. Key = `sha256(model + "\x00" + prompt)[:32]`. `SET NX EX 86400`. Fast, bounded, survives across worker processes.

**L2: Disk** — long-term, per-org (`{org_id}/{key_hash}.json`). Survives Redis flush; for re-runs and CI.

Cache is **per-org** to isolate tenants. Each entry stores `{prompt_hash, model, response, ts, token_usage}`. Atomic writes via tmp-rename (disk) and `SET NX EX` (Redis).

Lookup order in every node: `if state["use_cache"]: redis.get(key) or disk.get(key) or None`. On hit, token usage is restored from the cache entry (so billing is accurate). `use_cache: false` bypasses both. CI runs default to no-cache.

---

## Crawler Design — Three Implementations, One Interface

- `crawler/github.py`: uses the **user's OAuth token** (decrypted in memory for the run's duration, never logged). Git Trees API for discovery → Contents API for content. Respects `X-RateLimit-Remaining`; on 403 with rate-limit hit, marks run as `paused` and resumes after the reset.
- `crawler/local.py`: admin/dev only, behind a feature flag.
- `crawler/upload.py`: tarball upload (≤ 100 MB by default), extracted server-side to a temp dir, scanned the same way.

All return `List[FileEntry(path, content)]`; binary detection (NUL byte in first 8KB) and size cap (`max_file_size`, default 100 KB) are applied uniformly.

---

## API Surface (REST + WebSocket/SSE)

```
POST   /api/v1/auth/github                  → 302 to GitHub OAuth
GET    /api/v1/auth/github/callback         → sets httpOnly cookies
POST   /api/v1/auth/refresh
POST   /api/v1/auth/logout

GET    /api/v1/orgs                         → list orgs current user belongs to
GET    /api/v1/orgs/{org}/usage            → this period's quota usage

GET    /api/v1/orgs/{org}/projects          → list
POST   /api/v1/orgs/{org}/projects          → create (name, repo_url or upload)
GET    /api/v1/projects/{id}
DELETE /api/v1/projects/{id}

GET    /api/v1/projects/{id}/runs           → list
POST   /api/v1/projects/{id}/runs           → start a new run
GET    /api/v1/runs/{id}                    → status + metadata
POST   /api/v1/runs/{id}/cancel
GET    /api/v1/runs/{id}/events             → SSE stream of progress events
WS     /api/v1/runs/{id}/ws                 → WebSocket (alternative to SSE)

GET    /api/v1/runs/{id}/artifacts          → list (chapters + zip)
GET    /api/v1/artifacts/{id}               → signed download URL

GET    /api/v1/orgs/{org}/api-keys          → list (metadata only, no key value)
PUT    /api/v1/orgs/{org}/api-keys/{provider}  → upsert (encrypted at rest)
DELETE /api/v1/orgs/{org}/api-keys/{provider}

GET    /api/v1/healthz                      → liveness
GET    /api/v1/readyz                       → readiness (db + redis check)
```

OpenAPI spec auto-generated by FastAPI at `/api/v1/docs`.

---

## Frontend Surface (Next.js 14 App Router)

| Route | Purpose |
|---|---|
| `/login` | "Sign in with GitHub" button |
| `/dashboard` | Recent runs, quota usage, quick-start |
| `/projects` | List of projects for current org |
| `/projects/new` | Form: paste GitHub URL OR drag-drop tarball + name |
| `/projects/[id]` | Project detail: runs table, settings, delete |
| `/projects/[id]/runs/new` | Configure run: max abstractions, language, cache, then start |
| `/projects/[id]/runs/[runId]` | Live progress (timeline of nodes), token usage, cancel button |
| `/projects/[id]/runs/[runId]/files/[...path]` | Markdown viewer with rendered Mermaid, file tree sidebar |
| `/settings/api-keys` | Add/remove per-provider keys (BYO) |
| `/settings/billing` | Current usage, plan, upgrade |

**Live progress** uses `EventSource` against `/api/v1/runs/{id}/events` (SSE). Events:
```json
{ "type": "node.started", "node": "build_code_graph", "ts": "..." }
{ "type": "node.completed", "node": "build_code_graph", "duration_ms": 1234 }
{ "type": "log", "level": "info", "msg": "..." }
{ "type": "chapter.completed", "index": 1, "name": "..." }
{ "type": "run.completed", "artifacts": [...] }
{ "type": "run.failed", "error": "..." }
```

**Mermaid rendering**: client-side via `mermaid.js` initialized in a `MermaidViewer` component. Falls back to a `<pre>` block if parsing fails.

**Markdown rendering**: `react-markdown` + `remark-gfm` + `rehype-mermaid`. Code excerpts get syntax highlighting via `rehype-highlight`.

---

## Production Readiness — The Full Checklist

### Security
- [x] HTTPS only (TLS termination at ingress)
- [x] Secrets via env (`pydantic-settings`), never in code
- [x] API keys encrypted at rest with Fernet (key from `APP_SECRET_KEY`)
- [x] GitHub OAuth tokens decrypted only in worker memory, scoped to the run, never logged
- [x] CSRF protection on session cookies (SameSite=Lax, Secure, httpOnly)
- [x] Rate limiting per IP and per user (e.g. `slowapi`)
- [x] Input validation on all endpoints (pydantic)
- [x] CORS allowlist (no `*` in prod)
- [x] SQL injection impossible via SQLAlchemy parameterized queries
- [x] Tarball uploads validated (magic bytes, nested-archive bomb check, size cap)
- [x] Dependency scanning in CI (`pip-audit`, `npm audit`)

### Reliability
- [x] Async job queue (Arq) with retries (max 3, exponential backoff)
- [x] Per-run checkpoint: persist state to DB after each node so a crashed run can resume from the last completed node
- [x] Graceful shutdown of workers (finish current node, then exit)
- [x] Idempotent job IDs (re-enqueuing the same run does nothing)
- [x] DB connection pooling (asyncpg pool, sized for worker count)
- [x] Redis persistence (RDB + AOF)
- [x] Postgres backups (pg_dump nightly, WAL archiving)

### Observability
- [x] Structured logs (`structlog` → JSON → Loki/CloudWatch)
- [x] Prometheus metrics: `runs_total{status}`, `run_duration_seconds`, `tokens_used_total{org,model}`, `cache_hit_ratio`, `queue_depth`, `http_request_duration_seconds`, `chapter_parallelism`
- [x] OpenTelemetry traces across API → worker → LLM (langfuse optional for LLM traces)
- [x] `/healthz` (liveness) + `/readyz` (readiness with DB+Redis ping)
- [x] Per-run log stream accessible in UI

### Multi-tenancy
- [x] All queries scoped by `org_id` (enforced at the DB layer with RLS or in the repository pattern)
- [x] Per-org quotas (max runs/month, max repo size MB, max concurrent runs)
- [x] Per-org LLM API keys (BYO; we never proxy)
- [x] Per-org cache namespaces

### Cost controls
- [x] Token budget per run (hard cap, configurable per org tier)
- [x] Repo size limit per plan
- [x] Concurrent run limit per org
- [x] Per-run cost estimate returned to the user before they confirm
- [x] Webhook on `run.completed` with cost + token usage

### Deployment
- [x] Docker images: `api`, `worker`, `web` (multi-stage builds; non-root users)
- [x] `docker-compose.yml` for dev (postgres, redis, minio, api, worker, web)
- [x] Kubernetes manifests (or Fly.io / Railway for simpler prod)
- [x] GitHub Actions CI: lint, type-check (mypy strict + tsc), test (pytest + Playwright), build images
- [x] GitHub Actions CD: build + push images on tag; migrations applied before deploy
- [x] Health checks in compose + k8s

### Documentation
- [x] `README.md` — quickstart, env vars, design overview
- [x] `docs/architecture.md` — C4 diagrams (mermaid) for context/container/component/deployment views
- [x] `docs/api.md` — OpenAPI mirror with examples
- [x] `docs/runbook.md` — common ops: scaling workers, debugging a stuck run, rotating secrets, restoring from backup

---

## Critical Files to Create (In Build Order)

### Backend
1. `docker-compose.yml`, `.env.example`, `pyproject.toml` — project skeleton.
2. `src/codebase_kb/config.py` — pydantic-settings env loader.
3. `src/codebase_kb/db/{session,models}.py` + Alembic init + first migration.
4. `src/codebase_kb/auth/{github_oauth,jwt,permissions}.py` — auth layer.
5. `src/codebase_kb/llm/{base,gemini,anthropic,openai_compat,ollama,router}.py` — LLM layer + per-org router.
6. `src/codebase_kb/cache.py` — two-tier cache.
7. `src/codebase_kb/codeintel/{ast_python,graph,slicing,models}.py` — **the new core**.
8. `src/codebase_kb/crawler/{github,local,upload,models}.py` — file fetching.
9. `src/codebase_kb/prompts/{identify,analyze,order,write_chapter}.md` — prompt templates.
10. `src/codebase_kb/output/{mermaid,writer,zip}.py` — file writing and diagrams.
11. `src/codebase_kb/graph/{state,graph}.py` + `graph/nodes/{fetch_repo,build_code_graph,identify_abstractions,analyze_relationships,order_chapters,write_chapters,combine_tutorial}.py` — the 7 workers.
12. `src/codebase_kb/workers/{tasks,arq_settings,progress}.py` — async job execution.
13. `src/codebase_kb/api/v1/{projects,runs,artifacts,billing,webhooks}.py`, `api/ws.py` — REST + WS.
14. `src/codebase_kb/main.py` — FastAPI app factory.
15. `src/codebase_kb/observability/{logging,metrics,tracing}.py` — ops.
16. `tests/...` — unit + integration + e2e.

### Frontend
1. `package.json`, `next.config.mjs`, `tailwind.config.ts` — project skeleton.
2. `src/lib/{api-client,auth,ws}.ts` — typed wrappers.
3. `src/components/{mermaid-viewer,markdown-viewer,run-progress,file-tree,repo-input}.tsx` — UI primitives.
4. `src/app/(auth)/login/page.tsx`, `src/app/api/auth/callback/route.ts` — auth flow.
5. `src/app/(app)/dashboard/page.tsx` — landing.
6. `src/app/(app)/projects/{page,new/page,[id]/page}.tsx` — project CRUD.
7. `src/app/(app)/projects/[id]/runs/{[runId]/page,new/page}.tsx` — run lifecycle.
8. `src/app/(app)/projects/[id]/runs/[runId]/files/[...path]/page.tsx` — result viewer.
9. `src/app/(app)/settings/{api-keys,billing}/page.tsx` — org settings.
10. `tests/...` — Playwright e2e.

### Infra
1. `infra/docker/{api,worker,web}.Dockerfile` — multi-stage builds.
2. `infra/github-actions/{ci,cd}.yml` — pipelines.
3. `infra/k8s/*.yaml` — optional: deployment, service, ingress, HPA.

**Why this order**: each file depends on the ones above it. `config.py` and `db/` are foundational. `codeintel` is independent of auth and can be built in parallel. `graph/` is the last "code" file because it imports from everything.

---

## How to Build This (Step-by-Step)

### Phase 1: Project Skeleton
1. Create the monorepo folder structure.
2. Write `docker-compose.yml` with postgres, redis, minio, api, worker, web.
3. Write `.env.example` with all env var names and a comment explaining each.
4. Initialize git, make first commit.

### Phase 2: Database + Auth
1. Define SQLAlchemy models (User, Org, OrgMember, Project, Run, Artifact, ApiKey, Usage, Webhook, Quota).
2. Generate Alembic migration; run against the compose postgres.
3. Implement GitHub OAuth flow.
4. Implement JWT issuance + httpOnly cookie middleware.
5. Implement RBAC dependency (`get_current_user`, `get_org_for_user`).

### Phase 3: LLM Layer + Cache
1. Define `LLMProvider` Protocol.
2. Implement Gemini/Anthropic/OpenAI-compat/Ollama providers.
3. Implement `router.get_provider_for_org()`.
4. Implement two-tier cache (Redis L1 + disk L2).
5. Test with a one-line prompt end-to-end.

### Phase 4: CodeIntel Layer (the new core)
1. Implement `codeintel/models.py` (CodeNode, CodeEdge dataclasses).
2. Implement `codeintel/ast_python.py` (AST → nodes/edges).
3. Implement `codeintel/graph.py` (NetworkX wrapper, metrics, slicing).
4. Implement `codeintel/slicing.py` (token-budgeted prompt builder).
5. Unit test on a hand-built DAG; verify PageRank and topo sort.

### Phase 5: Crawlers
1. Define `FileEntry` dataclass.
2. Implement `crawler/local.py` (admin/dev) first — easiest to test.
3. Implement `crawler/github.py` with user's OAuth token + rate-limit handling.
4. Implement `crawler/upload.py` (tarball, size cap, bomb check).
5. Add binary detection and size cap to all three.

### Phase 6: Prompts + Mermaid
1. Write 4 prompt templates with new "input is pre-sliced, references are real" preamble.
2. Implement `mermaid.py` with sanitization rules + validation.
3. Unit test with adversarial inputs (unicode, reserved keywords, empty labels).

### Phase 7: Pipeline Nodes
1. `fetch_repo_node` — call crawler, write to DB, return `files` + `output_dir`.
2. `build_code_graph_node` — call codeintel, persist graph JSON to DB.
3. `identify_abstractions_node` — feed PageRank top-K to LLM, parse YAML.
4. `analyze_relationships_node` — feed graph edges + abstraction descriptions, parse YAML.
5. `order_chapters_node` — topo sort, optionally refine with LLM.
6. `write_chapter_single` — slice context, call LLM, return one chapter.
7. `combine_tutorial_node` — build overview diagram, write files, zip, upload to S3.

### Phase 8: Graph + Workers
1. Implement `build_graph()` with the 7 nodes + `Send` fan-out.
2. Implement Arq worker that calls `graph.invoke()` and publishes progress to Redis.
3. Implement progress publisher (writes events to `runs:{id}:events` channel).

### Phase 9: API + SSE
1. Implement REST routers (projects, runs, artifacts, api-keys, billing, webhooks).
2. Implement SSE endpoint that subscribes to Redis pub/sub and forwards events.
3. Add OpenAPI examples and tags.

### Phase 10: Frontend
1. Set up Next.js with shadcn/ui, Tailwind, TypeScript.
2. Build the auth flow.
3. Build the dashboard, projects list, project create form.
4. Build the run lifecycle pages (new, live progress, result viewer).
5. Build the settings pages (api-keys, billing).
6. Build the file viewer with Mermaid.js.

### Phase 11: Tests + Verification
1. Create `tests/fixtures/tiny_repo/` (10-file Python toy: auth/service/repo/api).
2. Unit tests for codeintel (AST round-trip, PageRank, slicing).
3. Integration test for the full pipeline with mocked LLM.
4. API tests for project/run lifecycle, auth, quota.
5. Playwright e2e test for the happy path.

### Phase 12: Production Hardening
1. Add rate limiting middleware.
2. Add healthz + readyz.
3. Add Prometheus metrics + OpenTelemetry traces.
4. Add structured logging.
5. Write `docs/runbook.md` (common ops scenarios).
6. Write `docs/architecture.md` (C4 diagrams).

### Phase 13: Deployment
1. Write Dockerfiles (multi-stage, non-root).
2. Write CI workflow (lint, type-check, test, build).
3. Write CD workflow (push images on tag, run migrations).
4. Write `infra/k8s/` manifests (or set up Fly.io / Railway).
5. Smoke test on a small public repo with the user's own Anthropic key.

---

## Verification — How to Know It Works

### Unit / Integration (pytest)

- **codeintel**: AST → graph round-trip on known fixtures; PageRank returns expected order on a hand-built DAG; `sliced_context` returns the right files for a sample anchor; cycle-breaking in topo sort is stable.
- **graph pipeline (end-to-end on `tiny_repo`)**: with cache off and a mocked LLM fixture returning canned YAML, the pipeline produces a valid `index.md` + chapter files + diagram.
- **API**: project create → run start → run status (`queued` → `running` → `succeeded`) → artifact list → signed download URL.
- **Auth**: GitHub OAuth callback sets cookies; protected endpoints reject anonymous; per-org isolation holds (org A cannot see org B's runs).
- **Quota**: a 6th run by an org on the free plan is rejected with 402.
- **Cache**: a re-run with cache enabled finishes in <5s and is byte-identical to the first run.

### E2E (Playwright)

1. Sign in with GitHub (mocked OAuth).
2. Create a project pointing at `tiny_repo` fixture URL.
3. Start a run; assert live progress events arrive over SSE in the UI.
4. On completion, navigate to the result viewer; assert the rendered Mermaid is present and the chapter list is populated.
5. Download the zip; assert the file exists with the expected contents.

### Manual smoke

1. Run against a small public repo (e.g. `pallets/flask`) with the user's own Anthropic key.
2. Inspect the tutorial; confirm every `file:line` excerpt actually exists in the repo (`grep -n`).
3. Confirm the overview Mermaid renders in GitHub's markdown view.
4. Check the token usage on the run record matches what your LLM dashboard reports (±5%).
5. Confirm a re-run with cache enabled finishes in <5s.

### Negative paths to verify

- LLM returns malformed YAML → node raises → framework retries (max 3) → surfaces clear error in run record.
- All providers missing API keys → 422 at run start with hint to add a key in settings.
- Repo > size cap → 413 at project create.
- User cancels mid-run → worker stops after current node; partial artifacts marked as such.
- GitHub rate-limit hit → run status `paused`, auto-resumes on reset.
- Cache corruption → fall back to live call, log warning.
- Network flake during fetch → retried with exponential backoff.
- AST parse error on one bad file → file is skipped, warning logged, pipeline continues.

---

## Teaching Notes — Why These Design Choices

1. **SaaS over CLI**: a CLI is friction (install, configure, babysit). A SaaS is "click and wait" — lower activation cost, recurring revenue, multi-tenant economics. The pipeline logic is identical; the delivery is productized.

2. **LangGraph for orchestration**: `Send` and reducers are hard to get right by hand. LangGraph handles join semantics automatically — you don't write a "wait for all chapters" barrier, you just declare the edge.

3. **AST + NetworkX over whole-repo prompting**: LLMs hallucinate under context pressure and cost grows linearly with repo size. A pre-built graph lets us feed the LLM only the *minimal, structurally-relevant slice* per step. **Cost drops ~50×, accuracy goes up, diagrams are grounded in real edges.** This is the single most important design decision.

4. **Pluggable LLM layer with per-org BYO keys**: lock-in to one provider is a strategic risk and a regulatory liability. BYO keys mean we never proxy code, users pay their own bills, and switching providers is one click. The Protocol-based abstraction costs ~50 lines and saves a rewrite.

5. **Programmatic Mermaid generation**: LLMs are unreliable at emitting valid syntax for structured formats. Ask an LLM to write Mermaid and you'll get parse errors ~20% of the time. Ask it for *labels* and build the syntax yourself — 0% parse errors, same visual output. And now the edges come from the AST graph, so they're validated too.

6. **Two-tier cache (Redis + disk) per-org**: LLM API calls are the bottleneck. Redis handles the hot path fast; disk survives Redis flushes and works in CI without Redis. Per-org namespacing isolates tenants.

7. **Send fan-out for chapter parallelism**: 10 sequential LLM calls = 10× latency. 10 parallel via `Send` = 1× latency. With graph-sliced context (~10K tokens each), 10 chapters finish in ~20s wall-clock.

8. **State as TypedDict, not class**: TypedDict gives type hints + IDE autocomplete without class boilerplate. `total=False` means fields are added incrementally as the pipeline progresses — no need to pre-declare everything at init time.

9. **One node per file**: makes nodes independently testable. Unit-test `identify_abstractions` by feeding it a fake state with `files` and a `code_graph` and checking the output structure — no full pipeline run needed.

10. **Prompts as Markdown files**: prompts evolve. Keeping them in `.md` means non-Python devs can edit them, you can syntax-highlight them, and diffs are clean in PRs.

11. **GitHub OAuth over password auth**: every target user already has a GitHub account; the repos they want to analyze live there; the OAuth scope lets us read repo contents directly with their token.

12. **SSE over WebSocket for progress**: SSE is unidirectional (server → client), works over plain HTTP/2, auto-reconnects, and is dead-simple to consume in the browser. WebSocket only if you need bidirectional (e.g. "chat with your run" — future feature).

---

## Summary

You're building a **two-layer SaaS product**: a synchronous HTTP API on top of an asynchronous LangGraph pipeline that runs as background jobs in a worker pool. The pipeline has **7 nodes**, one of which (`build_code_graph`) is the new core that solves the token problem by building a NetworkX graph from AST-extracted nodes and edges. Every LLM call after that node is fed a **minimal, sliced context** derived from the graph.

**Key insights:**

- **Two-layer architecture** = FastAPI HTTP + LangGraph pipeline + Arq worker queue.
- **AST + NetworkX** = the token-budget and hallucination fix. PageRank for "what's important", topological sort for "what order", ego_graph for "what context".
- **Pluggable LLM with BYO keys** = Protocol-based abstraction + per-org router; we never proxy.
- **Programmatic Mermaid** = LLM provides labels, code builds syntax from validated graph edges.
- **Two-tier per-org cache** = Redis (hot) + disk (cold); deterministic re-runs, fast iteration.
- **Send fan-out** = N parallel chapter writers, reducer-merged.
- **GitHub OAuth** = lowest-friction auth; repos are one click away.
- **Production readiness** = observability, multi-tenancy, cost controls, security hardening, CI/CD, runbook.

Build in the order listed, test at each phase with the `tiny_repo` fixture, and the architecture will hold together. The graph is the single biggest leverage point — invest in `codeintel/` and the rest gets cheap.
