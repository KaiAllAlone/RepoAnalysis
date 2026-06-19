# Automated Codebase Knowledge Builder — SaaS Edition (Plan)

## Context

Onboarding to a large, undocumented repository is one of the most expensive bottlenecks in software engineering. New developers waste days manually tracing imports and reading files line-by-line to build a mental model of an unfamiliar codebase. Existing tools fail in different ways: wikis go stale, docstring generators are too granular, semantic search (Bloop, Sourcegraph) requires the developer to already know what to look for.

**Goal (pivot from CLI → SaaS)**: build a multi-tenant web application that ingests a Git repository (URL or uploaded tarball), reverse-engineers its architecture, and emits a structured, human-readable Markdown tutorial (`index.md` + numbered chapter files) with auto-generated Mermaid diagrams. Users authenticate with GitHub OAuth, point the tool at a repo, watch progress in real time, and browse the resulting tutorial in the browser or download it as a zip.

**Reference architecture**: [PocketFlow-Tutorial-Codebase-Knowledge](https://the-pocket.github.io/PocketFlow-Tutorial-Codebase-Knowledge/) — a 6-node linear pipeline (Fetch → Identify → Analyze → Order → Write (BatchNode) → Combine) that we will re-implement in **LangGraph** with a pluggable LLM layer, running as an asynchronous background job per analysis request.

**The token-budget problem (the reason for AST + NetworkX)**:

Naïvely feeding an entire repo into the LLM is the first thing any prototype tries. It works on toy repos (~10 files, ~5K LOC). It breaks on real-world repos:

- A 1,000-file codebase is ~500K LOC → ~50M tokens of source → ~$500 and 2 hours per single identify-relationships call.
- The LLM also **hallucinates** when context is large: it invents symbols, misattributes files, generates plausible-but-wrong relationship edges. The diagrams then lie, and the tutorial is worse than nothing.
- Even with 1M-token context windows, accuracy degrades sharply past ~200K tokens (lost-in-the-middle effect).

**The fix**: we never give the LLM the whole repo. We build a **deterministic, grounded representation** of the codebase first, then give the LLM only the **minimal, structurally-relevant slice** it needs for each step.

**The two tools that make this tractable**:

| Tool | Role | What it gives us |
|---|---|---|
| `ast` (stdlib) | Parse Python into a syntax tree | Imports, function/class signatures, call sites, inheritance, decorators — *ground truth*, no guessing |
| `networkx` | Build and analyze a code graph | PageRank (core abstractions), community detection (chapter groups), topological sort (teaching order), k-hop neighborhoods (sliced context) |

Together they let us:

1. **Identify abstractions deterministically** — start from PageRank top-K files/symbols rather than asking the LLM "what's important?" on a 50M-token dump.
2. **Extract relationships programmatically** — import edges and call edges come straight from the AST/graph; only human-readable *labels* need the LLM.
3. **Order chapters from the graph** — topological sort of the dependency DAG gives a valid teaching order for free; the LLM call only refines prose.
4. **Slice context per chapter** — when writing chapter N, we feed the LLM only the k-hop neighborhood of N's anchor symbols, not the whole repo.

The LLM is reduced from "architect" to "narrator" — it writes prose and labels for a structure we've already validated.

**User-confirmed choices**:

- Delivery: **Multi-tenant SaaS** (web app + API), not a CLI.
- Backend: **FastAPI** (async, OpenAPI auto-docs, easy LangGraph integration) + **PostgreSQL** + **Redis** (queue + cache).
- Workers: **Arq** (lightweight async task queue) or **Celery** (heavier, more batteries). Default: **Arq** for simplicity.
- Orchestration: **LangGraph** StateGraph + `Send` for per-chapter fan-out (same as before).
- LLM providers: **Pluggable** — Gemini, Anthropic, OpenAI, Ollama, OpenAI-compatible. The user (org) supplies their own API key — we never proxy.
- Output: **Markdown** (`index.md` + `NN_name.md` + Mermaid `flowchart TD` / `sequenceDiagram`) viewable in-browser, downloadable as zip, renderable via Mermaid.js.
- Frontend: **Next.js 14 (App Router) + TypeScript + Tailwind + shadcn/ui**.
- Auth: **GitHub OAuth** (most natural — users connect the account that owns the repos they want to analyze).
- Deployment: **Docker Compose** for dev, **Kubernetes manifests** (or Fly.io / Railway) for prod.

**Working directory**: `C:\Users\HP\OneDrive\Desktop\temp` (where this file lives; project will be scaffolded in a sub-folder).

---

## Pipeline Overview (per Analysis Run)

```
START
  └─> fetch_repo                  (worker: clone or upload, dedupe)
        └─> build_code_graph       (Python: ast → NetworkX DiGraph; TS: tree-sitter → DiGraph)
              └─> identify_abstractions    (graph-driven: PageRank top-K + LLM narrative)
                    └─> analyze_relationships  (hybrid: AST edges = programmatic, labels = LLM)
                          └─> order_chapters   (graph-driven: topo sort, refined by LLM)
                                └─> [Send] -> write_chapter_single (×N, parallel, sliced ctx)
                                      └─> combine_tutorial (programmatic Mermaid + zip)
                                            └─> END
```

State is a single `TypedDict`. The `chapters` field uses `Annotated[List, operator.add]` so parallel `Send` writes merge cleanly.

---

## AST + NetworkX Token Strategy (Why This Works)

**The graph we build** (per repo):

- **Nodes**: one per `Module`, per top-level `FunctionDef`/`AsyncFunctionDef`, per `ClassDef`. Each node has attrs: `file`, `lineno`, `end_lineno`, `kind`, `name`, `signature`, `docstring`, `complexity` (cyclomatic).
- **Edges (directed)**:
  - `imports`: module A → module B (from `Import`/`ImportFrom`)
  - `calls`: function A → function B (resolved via `ast.Call.func` + symbol table; unresolved = dotted-name candidates)
  - `inherits`: class A → class B (from `ClassDef.bases`)
  - `contains`: module A → function/class A.X
  - `decorates`: function A → function B (from `Decorator` nodes)

**What we compute for free**:

| Metric | Algorithm | Token-relevance |
|---|---|---|
| Top-K core abstractions | `networkx.pagerank(G)` | LLM identifies abstractions from a *short, ranked list* of candidate symbols, not the whole repo |
| Chapter grouping | `networkx.community.louvain_communities(G)` | Each community becomes a chapter group; intra-group context only |
| Teaching order | `networkx.topological_sort(DAG)` after cycle-break | No LLM call needed for ordering — graph gives it for free |
| Per-chapter sliced context | `nx.ego_graph(G, anchor, radius=k)` | When writing chapter N, feed only anchor's k-hop neighborhood (filtered to top-N nodes by PageRank inside) |
| Cycle detection | `nx.find_cycle()` or `simple_cycles()` | Highlights architectural smells; becomes a chapter section |

**Token budget math** (illustrative for a 1000-file Python repo):

| Approach | Tokens fed to LLM per chapter | Total cost | Quality |
|---|---|---|---|
| Whole repo dump (naïve) | ~500K per call × 15 calls | ~$15/run, 90 min | Hallucinates, diagrams lie |
| AST summary only | ~50K per call × 15 calls | ~$1.50/run, 9 min | Better, but lacks context for prose |
| **Graph-sliced (our approach)** | ~10K per call × 15 calls | **~$0.30/run, ~3 min** | Best — every excerpt is real, every edge is grounded |

**Quality wins**: the LLM can no longer invent a relationship between files that don't import each other, or quote a function that doesn't exist. The graph is the source of truth.

---

## Directory Layout (Monorepo)

```
codebase-kb/
├── README.md                          # Quickstart, env vars, design overview
├── docker-compose.yml                 # postgres, redis, api, worker, web (dev)
├── .env.example                       # All env vars documented
├── pyproject.toml                     # Backend deps (uv / poetry)
├── package.json                       # Frontend deps (pnpm)
├── Makefile                           # Common dev commands
│
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   │   └── codebase_kb/
│   │       ├── __init__.py
│   │       ├── main.py                # FastAPI app factory
│   │       ├── config.py              # pydantic-settings
│   │       ├── deps.py                # FastAPI dependencies (db, current_user, provider)
│   │       ├── db/
│   │       │   ├── __init__.py
│   │       │   ├── session.py         # async engine, sessionmaker
│   │       │   ├── models.py          # SQLAlchemy: User, Org, Project, Run, Artifact, ApiKey
│   │       │   └── migrations/        # Alembic
│   │       ├── auth/
│   │       │   ├── github_oauth.py    # OAuth flow
│   │       │   ├── jwt.py             # access/refresh tokens
│   │       │   └── permissions.py     # RBAC: owner/admin/member/viewer
│   │       ├── api/
│   │       │   ├── v1/
│   │       │   │   ├── projects.py    # CRUD: create, list, get, delete
│   │       │   │   ├── runs.py        # start, list, get, cancel, stream (SSE)
│   │       │   │   ├── artifacts.py   # list, download (zip)
│   │       │   │   ├── webhooks.py    # run.completed, run.failed
│   │       │   │   └── billing.py     # quota check, usage
│   │       │   └── ws.py              # WebSocket /runs/{id}/progress
│   │       ├── workers/
│   │       │   ├── __init__.py
│   │       │   ├── arq_settings.py    # Arq Redis settings
│   │       │   ├── tasks.py           # enqueue_run, run_analysis
│   │       │   └── progress.py        # publish progress to Redis pub/sub → WS
│   │       ├── graph/
│   │       │   ├── __init__.py
│   │       │   ├── state.py           # KnowledgeBuilderState TypedDict
│   │       │   ├── graph.py           # build_graph() StateGraph + Send
│   │       │   └── nodes/
│   │       │       ├── fetch_repo.py
│   │       │       ├── build_code_graph.py     # NEW: ast → NetworkX
│   │       │       ├── identify_abstractions.py
│   │       │       ├── analyze_relationships.py
│   │       │       ├── order_chapters.py
│   │       │       ├── write_chapters.py
│   │       │       └── combine_tutorial.py
│   │       ├── codeintel/            # NEW: the AST/NetworkX layer
│   │       │   ├── __init__.py
│   │       │   ├── ast_python.py      # ast.parse → structural nodes/edges
│   │       │   ├── ast_typescript.py  # tree-sitter (later)
│   │       │   ├── graph.py           # NetworkX wrapper, metrics, slicing
│   │       │   ├── slicing.py         # ego_graph, page-rank-filtered context
│   │       │   └── models.py          # CodeNode, CodeEdge dataclasses
│   │       ├── crawler/
│   │       │   ├── github.py          # GitHub API (uses user's OAuth token)
│   │       │   ├── local.py           # admin/dev path
│   │       │   └── upload.py          # tarball upload, dedupe, size cap
│   │       ├── llm/
│   │       │   ├── base.py            # LLMProvider Protocol
│   │       │   ├── gemini.py
│   │       │   ├── anthropic.py
│   │       │   ├── openai_compat.py
│   │       │   ├── ollama.py
│   │       │   └── router.py          # per-org API key lookup
│   │       ├── cache.py               # DiskCache + RedisCache
│   │       ├── prompts/
│   │       │   ├── identify.md
│   │       │   ├── analyze.md
│   │       │   ├── order.md
│   │       │   └── write_chapter.md
│   │       ├── output/
│   │       │   ├── mermaid.py
│   │       │   ├── writer.py          # writes to local + uploads to S3
│   │       │   └── zip.py             # produces downloadable zip
│   │       ├── observability/
│   │       │   ├── logging.py         # structlog
│   │       │   ├── metrics.py         # prometheus_client
│   │       │   └── tracing.py         # opentelemetry (langfuse optional)
│   │       └── utils/
│   │           ├── yaml_parse.py
│   │           ├── hashing.py
│   │           └── tokens.py          # tiktoken wrapper for budget checks
│   └── tests/
│       ├── conftest.py                # fixtures: db, redis, fake_llm, sample_repo
│       ├── unit/
│       │   ├── test_ast_python.py
│       │   ├── test_graph_metrics.py
│       │   ├── test_slicing.py
│       │   ├── test_mermaid.py
│       │   └── test_cache.py
│       ├── integration/
│       │   ├── test_graph_pipeline.py # end-to-end on fixture repo
│       │   ├── test_api_projects.py
│       │   ├── test_api_runs.py
│       │   └── test_auth.py
│       └── fixtures/
│           └── tiny_repo/             # 10-file Python toy (auth/service/repo/api)
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── next.config.mjs
│   ├── tailwind.config.ts
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx              # marketing/landing
│   │   │   ├── (auth)/
│   │   │   │   └── login/page.tsx
│   │   │   ├── (app)/
│   │   │   │   ├── dashboard/
│   │   │   │   ├── projects/
│   │   │   │   │   ├── page.tsx      # list
│   │   │   │   │   ├── new/page.tsx  # create (paste URL / upload)
│   │   │   │   │   └── [id]/
│   │   │   │   │       ├── page.tsx  # project detail
│   │   │   │   │       └── runs/
│   │   │   │   │           └── [runId]/
│   │   │   │   │               ├── page.tsx     # live progress + result viewer
│   │   │   │   │               └── files/[...path]/page.tsx  # chapter view
│   │   │   │   └── settings/
│   │   │   │       ├── api-keys/page.tsx
│   │   │   │       └── billing/page.tsx
│   │   │   └── api/
│   │   │       └── auth/callback/route.ts
│   │   ├── components/
│   │   │   ├── ui/                   # shadcn primitives
│   │   │   ├── mermaid-viewer.tsx    # renders Mermaid.js from string
│   │   │   ├── markdown-viewer.tsx   # react-markdown + remark-gfm + rehype-mermaid
│   │   │   ├── run-progress.tsx      # subscribes to SSE/WS, shows node-by-node progress
│   │   │   ├── file-tree.tsx
│   │   │   └── repo-input.tsx        # URL paste / drop tarball
│   │   ├── lib/
│   │   │   ├── api-client.ts         # typed fetch wrapper
│   │   │   ├── auth.ts
│   │   │   └── ws.ts                 # WebSocket / EventSource wrapper
│   │   └── styles/
│   └── tests/
│       └── ...                       # Playwright e2e
│
├── infra/
│   ├── docker/
│   │   ├── api.Dockerfile
│   │   ├── worker.Dockerfile
│   │   └── web.Dockerfile
│   ├── k8s/                          # optional: deployment, service, ingress
│   ├── terraform/                    # optional: prod infra
│   └── github-actions/
│       ├── ci.yml                    # lint, type-check, test, build
│       └── cd.yml                    # build + push images
│
└── docs/
    ├── design.md
    ├── architecture.md                # C4 diagrams (mermaid)
    ├── api.md                         # OpenAPI mirror
    └── runbook.md                     # ops: scaling, debugging, incident response
```

---

## Data Model (PostgreSQL)

```sql
-- Tenancy
users          (id, github_id, login, email, avatar_url, created_at)
orgs           (id, name, plan, created_at)
org_members    (org_id, user_id, role)   -- owner | admin | member | viewer

-- Resources
projects       (id, org_id, name, repo_url, default_branch, created_by, created_at)
runs           (id, project_id, status, started_at, finished_at, error,
                -- pipeline state snapshot for resume/debug:
                abstractions_json, relationships_json, chapter_order_json,
                token_usage_json, cost_cents)
artifacts      (id, run_id, kind, path, size_bytes, sha256, storage_url)
               -- kind: 'index' | 'chapter' | 'diagram' | 'zip'

-- Per-org config (BYO keys)
api_keys       (id, org_id, provider, encrypted_key, model, created_at, last_used_at)

-- Billing/quota
usage          (id, org_id, period_yyyymm, runs_count, tokens_in, tokens_out, cost_cents)
quotas         (org_id, max_runs_per_month, max_repo_size_mb, max_concurrent)
webhooks       (id, org_id, url, secret, events[])  -- 'run.completed', 'run.failed'
```

**Notes**:
- `runs.status` enum: `queued | running | succeeded | failed | cancelled`.
- API keys are encrypted at rest with `cryptography.fernet` (key from `APP_SECRET_KEY`).
- `artifacts.storage_url` is S3-compatible (or local `MinIO` in dev). Frontend signs a short-lived URL for download.
- `runs.token_usage_json` lets us show per-run cost and aggregate per-org usage for billing.

---

## State Shape (`backend/src/codebase_kb/graph/state.py`)

```python
class KnowledgeBuilderState(TypedDict, total=False):
    # --- inputs (set once when run starts) ---
    run_id: str
    project_id: str
    org_id: str
    repo_url: Optional[str]
    local_dir: Optional[str]                # admin/dev only
    project_name: str
    github_token: Optional[str]              # user's OAuth token (decrypted, in-memory only)
    output_dir: str                          # local worker temp dir
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
    code_graph: Dict[str, Any]               # NEW: serialized NetworkX graph (nodes/edges/metrics)
    abstractions: List[Dict[str, Any]]       # [{name, description, anchor_node_ids, file_indices}]
    relationships: List[Dict[str, Any]]      # [{from, to, label, kind}] — kind in {import,call,inherit,llm}
    chapter_order: List[int]                 # ordered abstraction indices

    # --- outputs ---
    chapters: List[Dict[str, Any]]           # [{index, name, markdown}, ...] (reducer-merged)
    final_output_dir: str
    token_usage: Dict[str, int]              # NEW: tracked per node for billing/observability
```

`chapters` is declared `Annotated[List[Dict[str, Any]], operator.add]` so `Send` workers can each return `{"chapters": [single]}` and the framework concatenates.

**Why `code_graph` is in state**: the next node reads it without rebuilding. Also lets us persist a snapshot for debugging runs that failed mid-pipeline.

---

## StateGraph Wiring (`backend/src/codebase_kb/graph/graph.py`)

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

`route_to_chapter_writers(state)`: for each `idx` in `state["chapter_order"]`, emit `Send("write_chapter_single", WriteChapterInput(abstraction_index=idx, code_graph=state["code_graph"], ...))`.

**Node read/write matrix**:

| Node | Reads | Writes | LLM? |
|---|---|---|---|
| fetch_repo | repo_url, github_token, filters | `files`, `output_dir` | no |
| **build_code_graph** | `files` | `code_graph` | no |
| identify_abstractions | `code_graph` (top-K PageRank), `files` | `abstractions` | yes (short prompt) |
| analyze_relationships | `abstractions`, `code_graph` (edges from graph are ground truth) | `relationships` | yes (labels only) |
| order_chapters | `code_graph` (topo sort), `abstractions` | `chapter_order` | optional (prose intro) |
| write_chapter_single | `code_graph` (ego_graph sliced), `abstractions[i]` | `chapters[i]` (append) | yes (sliced context) |
| combine_tutorial | `chapters`, `abstractions`, `relationships`, `code_graph` | filesystem writes, `final_output_dir`, artifact upload | no |

---

## CodeIntel Layer (`backend/src/codebase_kb/codeintel/`)

This is the **new, central piece** that solves the token problem.

### `ast_python.py`

```python
def parse_python_file(path: str, source: str) -> Tuple[List[CodeNode], List[CodeEdge]]:
    """Walk an AST and emit structural nodes and edges."""
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
            nodes.append(CodeNode(
                id=f"fn:{path}:{stmt.name}", kind="function",
                name=stmt.name, file=path,
                lineno=stmt.lineno, end_lineno=stmt.end_lineno or stmt.lineno,
                signature=ast.unparse(stmt.args),
                docstring=ast.get_docstring(stmt) or "",
            ))
            edges.append(CodeEdge(src=f"mod:{path}", dst=f"fn:{path}:{stmt.name}", kind="contains"))
            _walk_calls(stmt, f"fn:{path}:{stmt.name}", edges)
            for d in stmt.decorator_list:
                edges.append(CodeEdge(src=f"fn:{path}:{stmt.name}", dst=_dotted(d), kind="decorates"))
        elif isinstance(stmt, ast.ClassDef):
            nodes.append(CodeNode(
                id=f"cls:{path}:{stmt.name}", kind="class",
                name=stmt.name, file=path,
                lineno=stmt.lineno, end_lineno=stmt.end_lineno or stmt.lineno,
                signature=", ".join(ast.unparse(b) for b in stmt.bases),
                docstring=ast.get_docstring(stmt) or "",
            ))
            edges.append(CodeEdge(src=f"mod:{path}", dst=f"cls:{path}:{stmt.name}", kind="contains"))
            for base in stmt.bases:
                edges.append(CodeEdge(src=f"cls:{path}:{stmt.name}", dst=_dotted(base), kind="inherits"))
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes.append(CodeNode(
                        id=f"fn:{path}:{stmt.name}.{item.name}", kind="method",
                        name=f"{stmt.name}.{item.name}", file=path,
                        lineno=item.lineno, end_lineno=item.end_lineno or item.lineno,
                        signature=ast.unparse(item.args),
                        docstring=ast.get_docstring(item) or "",
                    ))
                    edges.append(CodeEdge(src=f"cls:{path}:{stmt.name}", dst=f"fn:{path}:{stmt.name}.{item.name}", kind="contains"))
    return nodes, edges
```

### `graph.py` — NetworkX wrapper

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

    def core_abstractions(self, k: int = 15) -> list[str]:
        """Top-K nodes by PageRank — the architecturally important ones."""
        return [n for n, _ in sorted(nx.pagerank(self.g).items(),
                                     key=lambda x: -x[1])[:k]]

    def communities(self) -> list[set[str]]:
        """Louvain communities — natural chapter groupings."""
        return nx.community.louvain_communities(self.g.to_undirected(), seed=42)

    def chapter_order_indices(self, abstraction_ids: list[str]) -> list[int]:
        """Topological sort over the abstraction DAG, breaking cycles by PageRank."""
        sub = self.g.subgraph(abstraction_ids).copy()
        # break cycles: drop lowest-PageRank back-edges until DAG
        pr = nx.pagerank(sub)
        for _ in range(1000):  # cap
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
        # filter to top-N by PageRank inside this slice
        sub = self.g.subgraph(nodes)
        pr = nx.pagerank(sub)
        keep = set(n for n, _ in sorted(pr.items(), key=lambda x: -x[1])[:max_nodes])
        return sorted({self.g.nodes[n]["file"] for n in keep})
```

### `slicing.py` — context builder for the LLM

```python
def build_chapter_prompt(anchor: dict, code_graph: CodeGraph,
                         files_by_path: dict[str, str],
                         token_budget: int = 10_000) -> str:
    """Build a prompt with ONLY the relevant files for this chapter."""
    paths = code_graph.sliced_context(anchor["anchor_node_ids"], radius=2)
    pieces = []
    used = 0
    for p in paths:
        content = files_by_path.get(p, "")
        cost = count_tokens(content)
        if used + cost > token_budget:
            # truncate with a marker; LLM gets the gist
            content = truncate_to_tokens(content, token_budget - used)
            pieces.append(f"# {p}\n{content}\n# [truncated]")
            break
        pieces.append(f"# {p}\n{content}")
        used += cost
    return "\n\n".join(pieces)
```

---

## LLM Provider Abstraction (unchanged in spirit, new in plumbing)

`LLMProvider` Protocol is the same as the original plan. The **new** part is the **router**: each org stores its own API key; the worker resolves the provider for a run by reading `api_keys` for the run's org.

```python
# llm/router.py
def get_provider_for_org(org_id: str) -> LLMProvider:
    key_row = db.fetch_one(
        "SELECT provider, encrypted_key, model FROM api_keys WHERE org_id = %s ORDER BY last_used_at DESC LIMIT 1",
        (org_id,),
    )
    if not key_row:
        raise NoAPIKeyError(f"Org {org_id} has no API keys configured")
    decrypted = fernet.decrypt(key_row.encrypted_key.encode()).decode()
    return _factory(key_row.provider, model=key_row.model, api_key=decrypted)
```

Users supply their own keys — we never proxy LLM calls through our servers. **This is also a regulatory win** (the user's source code never leaves their LLM provider).

---

## Prompt Strategy

Same as the original plan, with two **token-budget additions**:

| Prompt | Output shape | Drives | New constraint |
|---|---|---|---|
| `identify.md` | YAML list of `{name, description, file_indices: [int,...]}` | `abstractions` | Input = top-K PageRank candidates only (not the whole repo) |
| `analyze.md` | YAML `{summary, relationships: [{from, to, label, kind}]}` | `relationships` | Input = graph edges as ground truth; LLM only adds labels and `kind: 'semantic'` edges |
| `order.md` | YAML ordered list of concept names | `chapter_order` (refined) | Optional — graph topo sort is the primary order |
| `write_chapter.md` | Markdown tutorial | `chapters[i].markdown` | Input = sliced k-hop subgraph context only |

**The prompt template header changes** for identify and write_chapter: it tells the model the input is a pre-filtered slice, and that names/edges in the input are guaranteed to exist in the codebase. This both reduces hallucination and shortens the prompt.

---

## Mermaid Generation (unchanged)

`build_overview_diagram(abstractions, relationships) -> str` and `build_chapter_sequence(...) -> str` work the same way as in the original plan. The relationship edges now come from the graph (validated) plus optional LLM-supplied semantic labels. The diagrams are therefore **both grounded and labeled**.

Sanitization rules are unchanged:
1. Node IDs: `re.sub(r"[^A-Za-z0-9_]", "_", name)[:40]`; prefix `n` if leading digit; numeric suffix on collision.
2. Labels: escape `"` → `#quot;`, collapse `\n` → space, truncate to 60 chars + `…`; HTML-escape `<>|&`.
3. Edge labels: same rules; non-empty fallback `"uses"`.
4. Reserved-keyword guard: `end|subgraph|graph|class` → append `_node`.
5. Validation: regex check on first non-empty line; raise `MermaidGenError` and write the chapter without the diagram on failure (don't abort the pipeline).

---

## Caching Layer (extended)

**Two-tier cache** (Redis + disk):

- **L1: Redis** — TTL'd (24h) hot path. Key = `sha256(model + "\x00" + prompt)[:32]`. Fast, bounded.
- **L2: Disk** — long-term, per-org (`{org_id}/...`). Survives Redis flush; for re-runs and CI.

Cache is **per-org** to isolate tenants. Each entry stores `{prompt_hash, model, response, ts, token_usage}`. Atomic writes via tmp-rename (disk) and `SET NX EX` (Redis).

`--no-cache` flag (or `use_cache: false` in run config) bypasses both. CI runs by default have cache disabled.

---

## Crawler Design (new: user-supplied GitHub token)

- `crawler/github.py`: uses the **user's OAuth token** (decrypted in memory for the duration of the run). Git Trees API for discovery → Contents API for content. Respects `X-RateLimit-Remaining`; on 403 with rate-limit hit, marks run as `paused` and resumes after the reset.
- `crawler/local.py`: admin/dev only, behind a feature flag.
- `crawler/upload.py`: tarball upload (≤ 100 MB by default), extracted server-side to a temp dir, scanned the same way.

Both return `List[FileEntry(path, content)]`; binary detection (NUL byte in first 8KB) and size cap (`max_file_size`, default 100 KB) are applied uniformly.

---

## API Surface (REST + WebSocket)

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
WS     /api/v1/runs/{id}/ws                 → bidirectional (future: chat with run)

GET    /api/v1/runs/{id}/artifacts          → list (chapters + zip)
GET    /api/v1/artifacts/{id}               → signed download URL

GET    /api/v1/orgs/{org}/api-keys          → list (metadata only, no key)
PUT    /api/v1/orgs/{org}/api-keys/{provider}  → upsert (encrypted)
DELETE /api/v1/orgs/{org}/api-keys/{provider}

GET    /api/v1/healthz                     → liveness
GET    /api/v1/readyz                       → readiness (db + redis check)
```

OpenAPI spec auto-generated by FastAPI at `/api/v1/docs`.

---

## Frontend Surface (Next.js)

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
| `/settings/api-keys` | Add/remove per-provider keys |
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

---

## Production Readiness Checklist

### Security
- [x] HTTPS only (TLS termination at ingress)
- [x] Secrets via env (`pydantic-settings`), never in code
- [x] API keys encrypted at rest with Fernet (key from `APP_SECRET_KEY`)
- [x] GitHub OAuth tokens decrypted only in worker memory, scoped to the run, never logged
- [x] CSRF protection on session cookies (SameSite=Lax, Secure)
- [x] Rate limiting per IP and per user (slowapi / custom)
- [x] Input validation on all endpoints (pydantic)
- [x] CORS allowlist (no `*` in prod)
- [x] SQL injection impossible via SQLAlchemy parameterized queries
- [x] Tarball uploads scanned with `clamav` or quarantined (optional)

### Reliability
- [x] Async job queue (Arq) with retries (max 3, exponential backoff)
- [x] Per-run checkpoint: persist state to DB after each node so a crashed run can resume
- [x] Graceful shutdown of workers (finish current node, then exit)
- [x] Idempotent job IDs (re-enqueuing the same run does nothing)
- [x] DB connection pooling (asyncpg pool)
- [x] Redis sentinel/cluster for HA (prod)
- [x] Postgres backups (pg_dump nightly, WAL archiving)

### Observability
- [x] Structured logs (`structlog` → JSON → Loki/CloudWatch)
- [x] Prometheus metrics: `runs_total`, `runs_duration_seconds`, `tokens_used_total`, `cache_hit_ratio`, `queue_depth`, `http_request_duration_seconds`
- [x] OpenTelemetry traces across API → worker → LLM (langfuse optional for LLM traces)
- [x] `/healthz` (liveness) + `/readyz` (readiness with DB+Redis ping)
- [x] Per-run log stream accessible in UI

### Multi-tenancy
- [x] All queries scoped by `org_id` (enforced at the DB layer with RLS or in the repository pattern)
- [x] Per-org quotas (max runs/month, max repo size, max concurrent)
- [x] Per-org LLM API keys (BYO; we never proxy)
- [x] Per-org cache namespaces

### Cost controls
- [x] Token budget per run (hard cap, configurable per org tier)
- [x] Repo size limit per plan
- [x] Concurrent run limit per org
- [x] Per-run cost estimate returned to the user before they confirm
- [x] Webhook on `run.completed` with cost + token usage

### Deployment
- [x] Docker images: `api`, `worker`, `web` (multi-stage builds)
- [x] `docker-compose.yml` for dev (postgres, redis, minio, api, worker, web)
- [x] Kubernetes manifests (or Fly.io / Railway for simpler prod)
- [x] GitHub Actions CI: lint, type-check (mypy + tsc), test (pytest + Playwright), build images
- [x] GitHub Actions CD: build + push images on tag
- [x] Migrations applied via Alembic in CI step before deploy

### Documentation
- [x] `README.md` — quickstart, env vars, design overview
- [x] `docs/architecture.md` — C4 diagrams (mermaid)
- [x] `docs/api.md` — OpenAPI mirror with examples
- [x] `docs/runbook.md` — common ops: scaling workers, debugging a stuck run, rotating secrets

---

## Critical Files to Create (in build order)

**Backend (in dependency order):**
1. `docker-compose.yml`, `.env.example`, `backend/pyproject.toml`
2. `backend/src/codebase_kb/config.py` (pydantic-settings)
3. `backend/src/codebase_kb/db/{session,models}.py` + Alembic init + first migration
4. `backend/src/codebase_kb/auth/{github_oauth,jwt,permissions}.py`
5. `backend/src/codebase_kb/llm/{base,gemini,anthropic,openai_compat,ollama,router}.py`
6. `backend/src/codebase_kb/cache.py` (Redis + disk)
7. `backend/src/codebase_kb/codeintel/{ast_python,graph,slicing,models}.py` ← **the new core**
8. `backend/src/codebase_kb/crawler/{github,local,upload,models}.py`
9. `backend/src/codebase_kb/prompts/*.md` (4 files)
10. `backend/src/codebase_kb/output/{mermaid,writer,zip}.py`
11. `backend/src/codebase_kb/graph/state.py`, `graph/nodes/*.py`, `graph/graph.py`
12. `backend/src/codebase_kb/workers/{tasks,arq_settings,progress}.py`
13. `backend/src/codebase_kb/api/v1/{projects,runs,artifacts,billing,webhooks}.py`, `api/ws.py`
14. `backend/src/codebase_kb/main.py` (FastAPI app factory)
15. `backend/tests/...`

**Frontend (in dependency order):**
1. `frontend/package.json`, `next.config.mjs`, `tailwind.config.ts`
2. `frontend/src/lib/{api-client,auth,ws}.ts`
3. `frontend/src/components/{mermaid-viewer,markdown-viewer,run-progress,file-tree,repo-input}.tsx`
4. `frontend/src/app/(auth)/login/page.tsx`, `api/auth/callback/route.ts`
5. `frontend/src/app/(app)/dashboard/page.tsx`
6. `frontend/src/app/(app)/projects/{page,new/page,[id]/page}.tsx`
7. `frontend/src/app/(app)/projects/[id]/runs/{[runId]/page,new/page}.tsx`
8. `frontend/src/app/(app)/projects/[id]/runs/[runId]/files/[...path]/page.tsx`
9. `frontend/src/app/(app)/settings/{api-keys,billing}/page.tsx`
10. `frontend/tests/...` (Playwright e2e)

**Infra:**
1. `infra/docker/{api,worker,web}.Dockerfile`
2. `infra/github-actions/{ci,cd}.yml`
3. `infra/k8s/*.yaml` (optional)

---

## Verification

### Unit / integration (pytest)

- **codeintel**: AST → graph round-trip on known fixtures; PageRank returns expected order on a hand-built DAG; `sliced_context` returns the right files for a sample anchor.
- **graph pipeline (end-to-end on `tiny_repo`)**: with cache off and a mocked LLM fixture that returns canned YAML, the pipeline produces a valid `index.md` + chapter files + diagram.
- **API**: project create → run start → run status (`queued` → `running` → `succeeded`) → artifact list → signed download URL.
- **Auth**: GitHub OAuth callback sets cookies; protected endpoints reject anonymous; per-org isolation holds (org A cannot see org B's runs).
- **Quota**: a 6th run by an org on the free plan is rejected with 402.

### E2E (Playwright)

1. Sign in with GitHub (mocked OAuth).
2. Create a project pointing at `tiny_repo` fixture URL.
3. Start a run; assert live progress events arrive over SSE.
4. On completion, navigate to the result viewer; assert the rendered Mermaid is present and the chapter list is populated.
5. Download the zip; assert the file exists with the expected contents.

### Manual smoke

1. Run against a small public repo (e.g. `pallets/flask`) with the user's own Anthropic key.
2. Inspect the tutorial; confirm every file:line excerpt actually exists in the repo (`grep -n`).
3. Confirm the overview Mermaid renders in GitHub's markdown view.
4. Check that a re-run with the cache enabled finishes in <5s.

### Negative paths to verify

- LLM returns malformed YAML → node raises → framework retries (max 3) → surfaces clear error in run record.
- All providers missing API keys → 422 at run start with hint to add a key in settings.
- Repo > size cap → 413 at project create.
- User cancels mid-run → worker stops after current node; partial artifacts marked as such.
- GitHub rate-limit hit → run status `paused`, auto-resumes on reset.
- Cache corruption → fall back to live call, log warning.
- Network flake during fetch → retried with exponential backoff.

---

## Open Questions to Confirm Before Build

1. **Hosting**: Fly.io / Railway / Render / AWS / self-hosted K8s? (affects infra files)
2. **Billing provider**: Stripe (preferred) vs. Lemon Squeezy vs. manual invoicing?
3. **Storage**: S3 / R2 / MinIO for artifacts in dev?
4. **Multi-language AST**: do we ship Python-only first and add TypeScript via `tree-sitter` later, or both from day 1?
5. **Plan tiers**: free / pro / team / enterprise — what quotas per tier?
6. **Self-serve signup**: yes (with GitHub OAuth) or invite-only beta first?
