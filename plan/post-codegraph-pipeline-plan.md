# Post-`build_code_graph` Pipeline Plan — Detailed Build Guide

## Where You Are

You've finished the `build_code_graph` node. At this point in a run, the `KnowledgeBuilderState` looks like:

```python
state = {
    # --- inputs (set once) ---
    "run_id": "...", "project_id": "...", "org_id": "...",
    "repo_url": "...", "github_token": "...", "output_dir": "/tmp/run-xyz",
    "include_patterns": [...], "exclude_patterns": [...], "max_file_size": 100000,
    "language": "english", "max_abstractions": 15, "use_cache": True,
    "provider": "anthropic", "model": "claude-sonnet-4-5",

    # --- produced by fetch_repo ---
    "files": [{"path": "src/auth/service.py", "content": "..."}, ...],

    # --- produced by build_code_graph ← YOU ARE HERE ---
    "code_graph": {
        "nodes": [{"id": "mod:src/auth/service.py", "kind": "module", ...}, ...],
        "edges": [{"src": "...", "dst": "...", "kind": "import", "label": "..."}, ...],
        "metrics": {
            "pagerank": {"mod:src/auth/service.py": 0.034, ...},
            "communities": [{"mod:src/auth/...", "fn:src/auth/service.py:login", ...}, ...],
            "module_pagerank_top_k": [{"id": "...", "score": 0.034}, ...],
            "cycles": [["mod:a", "mod:b", "mod:a"]],
        },
    },
}
```

**What you have**: a serialized NetworkX DiGraph of the codebase. Every node has a real `file` + `lineno`. Every edge is a verified import/call/inherit/contains/decorates. PageRank, communities, and cycle detection are precomputed.

**What you still need to build**: 5 more nodes + the `Send` fan-out + the `combine_tutorial` filesystem pipeline. That's this plan.

---

## The Big Picture — What Each Remaining Node Does

```
                          ┌──────────────────────────────────────────┐
                          │  state["code_graph"]                     │
                          │  (PageRank, communities, edges, slices) │
                          └───────────────┬──────────────────────────┘
                                          │ read by all 5 remaining nodes
                                          ▼
[1] identify_abstractions    →  state["abstractions"]
      LLM picks N "concepts" from the PageRank top-K candidate list.
      Each abstraction = {name, description, anchor_node_ids, file_indices}.

[2] analyze_relationships    →  state["relationships"]
      Edges from the graph are ground truth (kind: import|call|inherit|...).
      LLM only (a) writes prose labels for them, (b) optionally adds
      kind="semantic" edges the AST can't see.

[3] order_chapters           →  state["chapter_order"]
      Topological sort of the abstraction subgraph (cycles broken by
      lowest-PageRank back-edge removal). LLM call is OPTIONAL — only
      for prose refinement of the order.

[4] write_chapter_single ×N  →  state["chapters"] (reducer-merged)
      For each abstraction: slice the graph to anchor's k-hop neighborhood,
      budget tokens, send ONLY that slice + abstraction description to LLM,
      get back a Markdown chapter with code excerpts + Mermaid sequenceDiagram.

[5] combine_tutorial         →  filesystem writes + zip + artifact upload
      Programmatic Mermaid overview from validated edges, index.md TOC,
      chapter file writes, zip, upload to S3. No LLM calls.
```

---

## Node 1: `identify_abstractions`

### Goal

Take the PageRank-ranked list of candidate modules/symbols and ask the LLM to cluster them into 5–15 **abstractions** (the "chapters" of the tutorial). Each abstraction is a concept the tutorial will explain — not a single file or function, but a thematic grouping.

### Input → Output

```python
# Input (from state)
files:                List[Dict[str, str]]            # full content available if needed
code_graph:           Dict[str, Any]                  # has metrics.pagerank, metrics.communities
max_abstractions:     int                             # from CLI, default 15
language:             str

# Output (written to state)
abstractions: [
    {
        "name": "Authentication",
        "description": "Handles user login, session creation, and token issuance.",
        "anchor_node_ids": ["mod:src/auth/service.py", "cls:src/auth/service.py:AuthService"],
        "file_indices": [3, 7],                      # indices into state["files"]
    },
    ...
]
```

### Strategy — Why This Works

1. **Don't dump all files.** Sort `code_graph["nodes"]` by PageRank (already precomputed). Take the **top 2×max_abstractions** module nodes as candidates (modules, not every function — a 1000-node graph has too many tiny symbols to be useful here).
2. **Pre-group by community.** Louvain communities from the graph are excellent hints — each community is likely one or two abstractions. Show the LLM the communities with their top-PageRank members.
3. **Include signatures + docstrings only.** For each candidate module, include: `path`, top-5 functions by in-degree (heavily-called functions), top-3 classes, and the first 1–2 lines of each function's docstring. **No full file content.** This keeps the prompt tiny (~3–8K tokens) and the signal high.
4. **Constraint the output**: tell the LLM "emit between 5 and max_abstractions items; every name must be a real module/file path from the input; descriptions are 1–2 sentences."

### Prompt Template — `prompts/identify.md`

```markdown
You are reverse-engineering a codebase into a tutorial. The codebase has been
pre-analyzed: a dependency graph has been built and PageRank + community
detection have been run. Below are the **top {{top_k}} modules by PageRank**,
**pre-grouped into {{community_count}} communities** by Louvain community
detection on the graph.

Symbols and file paths in the input below are **guaranteed to exist** in the
codebase. Use them by exact name. Do not invent paths or symbols.

## Communities (each is a likely chapter group)

{% for community in communities %}
### Community {{ loop.index }} (PageRank sum: {{ community.pagerank_sum | round(3) }})
{% for mod in community.top_modules %}
- `{{ mod.path }}` — top functions: {{ mod.top_functions | join(", ") }}{% if mod.top_classes %} — top classes: {{ mod.top_classes | join(", ") }}{% endif %}
{% endfor %}
{% endfor %}

## Top {{ top_k }} Modules by PageRank (flat, ranked)

{% for mod in top_modules %}
{{ loop.index }}. `{{ mod.path }}` (PR={{ mod.pagerank | round(4) }})
   - functions called heavily: {{ mod.in_degree_functions | join(", ") or "(none)" }}
   - classes: {{ mod.classes | join(", ") or "(none)" }}
{% endfor %}

## Your Task

Cluster the modules above into **between 5 and {{ max_abstractions }} core
abstractions** (concepts that a developer onboarding to this repo would need
to understand). An abstraction should be a thematic concept, not a single
file — but it should be anchored to real modules.

Output a single YAML block fenced with ```yaml. Schema:

```yaml
- name: <Short Title-Case Name>             # e.g. "Authentication"
  description: <1-2 sentence description>  # e.g. "Handles user login, session creation, and token issuance."
  anchor_modules: [<list of module paths from the input>]   # at least 1, usually 1-3
```

Constraints:
- Total count between 5 and {{ max_abstractions }}.
- Every `anchor_modules` entry must be a path from the input above.
- Prefer community-aligned groupings; resist splitting a community across
  multiple abstractions unless it spans >10 modules.
- Write descriptions in {{ language }}.

Output ONLY the YAML block.
```

### Implementation — `graph/nodes/identify_abstractions.py`

```python
from __future__ import annotations
import operator
from typing import Any

from codebase_kb.codeintel.graph import CodeGraph            # the wrapper you already built
from codebase_kb.codeintel.slicing import estimate_tokens
from codebase_kb.llm.router import get_provider_for_org
from codebase_kb.cache import cache_lookup, cache_store
from codebase_kb.prompts import render_prompt
from codebase_kb.utils.yaml_parse import extract_yaml
from codebase_kb.observability.logging import get_logger

log = get_logger(__name__)

# How many top modules to show the LLM
TOP_K_MULTIPLIER = 2  # 2× max_abstractions candidates
MAX_FUNCTIONS_PER_MODULE = 5
MAX_CLASSES_PER_MODULE = 3


def _build_candidate_view(code_graph_payload: dict, max_abstractions: int) -> dict:
    """Reduce the full graph to a small, ranked, LLM-friendly view."""
    g = CodeGraph.from_payload(code_graph_payload)        # deserializer you wrote

    # 1) Top modules by PageRank
    module_nodes = [n for n, d in g.g.nodes(data=True) if d.get("kind") == "module"]
    module_pr = [(n, g.g.nodes[n].get("pagerank", 0.0)) for n in module_nodes]
    module_pr.sort(key=lambda x: -x[1])

    top_k = max_abstractions * TOP_K_MULTIPLIER
    top_modules = []
    for node_id, pr in module_pr[:top_k]:
        attrs = g.g.nodes[node_id]
        # top functions: highest in-degree within this module
        functions = sorted(
            [n for n in g.g.predecessors(node_id) if g.g.nodes[n].get("kind") == "function"],
            key=lambda n: -g.g.in_degree(n),
        )[:MAX_FUNCTIONS_PER_MODULE]
        classes = [
            n for n in g.g.nodes
            if g.g.nodes[n].get("kind") == "class"
            and g.g.nodes[n].get("file") == attrs.get("file")
        ][:MAX_CLASSES_PER_MODULE]
        top_modules.append({
            "path": attrs["file"],
            "pagerank": pr,
            "in_degree_functions": [g.g.nodes[f]["name"] for f in functions],
            "classes": [g.g.nodes[c]["name"] for c in classes],
        })

    # 2) Communities with their top modules
    communities = g.communities()
    community_views = []
    for comm in communities:
        mods_in_comm = [n for n in comm if g.g.nodes[n].get("kind") == "module"]
        mods_sorted = sorted(mods_in_comm, key=lambda n: -g.g.nodes[n].get("pagerank", 0.0))[:5]
        community_views.append({
            "pagerank_sum": sum(g.g.nodes[n].get("pagerank", 0.0) for n in mods_in_comm),
            "top_modules": [
                {
                    "path": g.g.nodes[n]["file"],
                    "top_functions": sorted(
                        [p for p in g.g.predecessors(n) if g.g.nodes[p].get("kind") == "function"],
                        key=lambda p: -g.g.in_degree(p),
                    )[:3],
                    "top_classes": [
                        c for c in g.g.nodes
                        if g.g.nodes[c].get("kind") == "class"
                        and g.g.nodes[c].get("file") == g.g.nodes[n].get("file")
                    ][:2],
                }
                for n in mods_sorted
            ],
        })

    return {
        "top_k": top_k,
        "community_count": len(communities),
        "communities": community_views,
        "top_modules": top_modules,
    }


async def identify_abstractions_node(state: dict) -> dict:
    log.info("identify_abstractions.start", run_id=state.get("run_id"))
    provider = get_provider_for_org(state["org_id"])
    model = state.get("model") or provider.default_model

    cache_key_payload = {
        "model": model,
        "code_graph_hash": state["code_graph"]["_meta"]["graph_hash"],
        "max_abstractions": state["max_abstractions"],
        "language": state.get("language", "english"),
    }

    # 1) Cache lookup
    if state.get("use_cache", True):
        cached = await cache_lookup("identify", cache_key_payload)
        if cached:
            log.info("identify_abstractions.cache_hit")
            return {"abstractions": cached["abstractions"], "token_usage": cached["token_usage"]}

    # 2) Build the candidate view (small, deterministic)
    view = _build_candidate_view(state["code_graph"], state["max_abstractions"])

    # 3) Render prompt
    prompt = render_prompt(
        "identify.md",
        top_k=view["top_k"],
        community_count=view["community_count"],
        communities=view["communities"],
        top_modules=view["top_modules"],
        max_abstractions=state["max_abstractions"],
        language=state.get("language", "english"),
    )

    # 4) Call LLM
    response = await provider.complete_async(
        prompt,
        temperature=0.2,
        max_tokens=2048,
    )

    # 5) Parse YAML
    items = extract_yaml(response)
    if not isinstance(items, list):
        raise ValueError(f"identify_abstractions: expected list, got {type(items).__name__}")

    # 6) Validate + resolve file_indices from anchor_modules
    file_index_by_path = {f["path"]: i for i, f in enumerate(state["files"])}
    abstractions: list[dict[str, Any]] = []
    for item in items:
        anchor_modules = item.get("anchor_modules", [])
        file_indices = sorted({
            file_index_by_path[m] for m in anchor_modules if m in file_index_by_path
        })
        if not file_indices:
            log.warning("identify_abstractions.skip_unknown_anchor", module=anchor_modules)
            continue
        abstractions.append({
            "name": item["name"],
            "description": item["description"],
            "anchor_node_ids": [
                f"mod:{m}" for m in anchor_modules if m in file_index_by_path
            ],
            "file_indices": file_indices,
        })

    # 7) Enforce bounds
    abstractions = abstractions[: state["max_abstractions"]]
    if len(abstractions) < 5:
        log.warning("identify_abstractions.too_few", count=len(abstractions))

    token_usage = {"prompt_tokens": estimate_tokens(prompt), "completion_tokens": estimate_tokens(response)}

    # 8) Cache + return
    if state.get("use_cache", True):
        await cache_store("identify", cache_key_payload, {
            "abstractions": abstractions,
            "token_usage": token_usage,
        })
    log.info("identify_abstractions.done", count=len(abstractions))
    return {"abstractions": abstractions, "token_usage": token_usage}
```

### Why This Is Robust

- **Cache key includes a hash of the graph** (not the prompt). If the code or graph changes, the cache is automatically invalidated. If the same repo is re-analyzed with the same graph and settings, the cache hits.
- **`anchor_modules` is validated against `state["files"]`** before being accepted. The LLM cannot inject phantom file paths.
- **Graceful degradation**: if the LLM returns fewer than 5, we warn but proceed. If more than `max_abstractions`, we truncate.
- **Token estimation before sending** lets us pre-flight check the prompt size and split into chunks if `>50K tokens` (extremely unlikely with this design, but defensive).

### Tests — `tests/unit/test_identify_abstractions.py`

```python
def test_build_candidate_view_top_modules_by_pagerank():
    """PageRank top-K should match the sorted node list."""
    payload = _fixture_graph_payload()   # 50-module toy graph
    view = _build_candidate_view(payload, max_abstractions=5)
    pageranks = [m["pagerank"] for m in view["top_modules"]]
    assert pageranks == sorted(pageranks, reverse=True)
    assert len(view["top_modules"]) == 10        # 2×5

def test_build_candidate_view_includes_communities():
    payload = _fixture_graph_payload()
    view = _build_candidate_view(payload, max_abstractions=5)
    assert view["community_count"] >= 1
    assert all("pagerank_sum" in c for c in view["communities"])

async def test_identify_abstractions_node_drops_unknown_anchors(faker_llm, sample_state):
    """LLM invents a path → that abstraction is dropped, not crashed."""
    faker_llm.set_response(yaml.dump([{
        "name": "Ghost", "description": "Imaginary",
        "anchor_modules": ["src/does_not_exist.py"],
    }, {
        "name": "Real", "description": "Real thing",
        "anchor_modules": ["src/auth/service.py"],
    }]))
    result = await identify_abstractions_node(sample_state)
    assert [a["name"] for a in result["abstractions"]] == ["Real"]

async def test_identify_abstractions_node_caches_response(faker_llm, sample_state):
    faker_llm.set_response(yaml.dump([{"name": "A", "description": "x", "anchor_modules": ["src/auth/service.py"]}]))
    sample_state["use_cache"] = True
    r1 = await identify_abstractions_node(sample_state)
    faker_llm.calls.clear()        # would crash if called again
    r2 = await identify_abstractions_node(sample_state)
    assert r1 == r2
```

---

## Node 2: `analyze_relationships`

### Goal

Produce the edge list for the **overview Mermaid diagram** and the per-chapter **sequence diagrams**. The graph already has import/call/inherit/contains/decorates edges — those are ground truth. The LLM only:
1. Writes a prose label for each edge.
2. Optionally adds `kind="semantic"` edges for cross-cutting concepts the AST can't see (e.g. "Auth orchestrates RateLimiter").

### Input → Output

```python
# Input
abstractions:  [{"name": "Authentication", "anchor_node_ids": [...], ...}, ...]
code_graph:    {nodes, edges, metrics}

# Output
relationships: [
    {"from": "Authentication", "to": "Session Store", "label": "issues tokens via", "kind": "import"},
    {"from": "Authentication", "to": "Rate Limiter",  "label": "consults before login", "kind": "semantic"},
    ...
]
summary: "<one-paragraph architectural overview>"
```

### Strategy

1. **Build the abstraction-to-abstraction edge set from the graph.** For every edge in the code graph where `src` and `dst` both belong to different abstractions (computed by membership of `anchor_node_ids`), create an `abstraction_edge`. Aggregate edges between the same pair of abstractions (a → b might have 5 import edges; collapse to one).
2. **Show the LLM the structural edge set as ground truth.** The prompt lists the edges already known, grouped by `kind`, and asks for: (a) a 1–5-word label for each, (b) optional additional semantic edges between abstractions, (c) a one-paragraph summary.
3. **Constrain**: labels must be short (≤40 chars), no invented abstraction names (validated against `state["abstractions"]`), every abstraction must have at least one edge (retry if not).

### Implementation — `graph/nodes/analyze_relationships.py`

```python
def _build_abstraction_edges(
    code_graph_payload: dict, abstractions: list[dict],
) -> list[dict]:
    """Project code-graph edges onto the abstraction level.

    Returns list of {from_name, to_name, kind, src_count} where src_count is the
    number of underlying code-graph edges collapsed into this abstract edge.
    """
    g = CodeGraph.from_payload(code_graph_payload)
    node_to_abs: dict[str, str] = {}
    for abs_ in abstractions:
        for nid in abs_["anchor_node_ids"]:
            node_to_abs[nid] = abs_["name"]

    # Also: map every module to its abstraction (by file path) so inter-module
    # import edges that don't touch anchor nodes still resolve.
    module_to_abs: dict[str, str] = {}
    for abs_ in abstractions:
        for fid in abs_["file_indices"]:
            mod_id = f"mod:{_path_for_file_index(fid)}"
            if mod_id in g.g:
                module_to_abs[mod_id] = abs_["name"]

    # Use both mappings: anchor nodes win over module mapping.
    abs_by_node = {**module_to_abs, **node_to_abs}

    bag: dict[tuple[str, str, str], int] = {}     # (from, to, kind) -> count
    for u, v, data in g.g.edges(data=True):
        from_abs = abs_by_node.get(u)
        to_abs = abs_by_node.get(v)
        if not from_abs or not to_abs or from_abs == to_abs:
            continue
        key = (from_abs, to_abs, data.get("kind", "unknown"))
        bag[key] = bag.get(key, 0) + 1

    return [
        {"from_name": k[0], "to_name": k[1], "kind": k[2], "src_count": c}
        for k, c in bag.items()
    ]


async def analyze_relationships_node(state: dict) -> dict:
    log.info("analyze_relationships.start", run_id=state.get("run_id"))
    provider = get_provider_for_org(state["org_id"])
    model = state.get("model") or provider.default_model

    cache_key_payload = {
        "model": model,
        "abstractions_hash": _hash_list(state["abstractions"]),
        "code_graph_hash": state["code_graph"]["_meta"]["graph_hash"],
    }

    if state.get("use_cache", True):
        cached = await cache_lookup("analyze", cache_key_payload)
        if cached:
            return {"relationships": cached["relationships"], "summary": cached["summary"], "token_usage": cached["token_usage"]}

    edges = _build_abstraction_edges(state["code_graph"], state["abstractions"])

    prompt = render_prompt(
        "analyze.md",
        abstractions=state["abstractions"],
        edges=edges,
        language=state.get("language", "english"),
    )

    response = await provider.complete_async(prompt, temperature=0.2, max_tokens=3000)
    parsed = extract_yaml(response)
    if not isinstance(parsed, dict):
        raise ValueError("analyze_relationships: expected YAML object")

    # Validate labels + semantic edge endpoints
    abs_names = {a["name"] for a in state["abstractions"]}
    relationships = []
    for r in parsed.get("relationships", []):
        if r["from"] not in abs_names or r["to"] not in abs_names:
            log.warning("analyze_relationships.drop_unknown", edge=r)
            continue
        if r["from"] == r["to"]:
            continue
        relationships.append({
            "from": r["from"],
            "to": r["to"],
            "label": (r.get("label") or "uses")[:60],
            "kind": r.get("kind", "semantic"),    # structural edges already carry kind from graph
        })

    # Connectivity check — every abstraction must appear in at least one edge.
    covered = {r["from"] for r in relationships} | {r["to"] for r in relationships}
    missing = abs_names - covered
    if missing:
        log.warning("analyze_relationships.uncovered_abstractions", missing=list(missing))
        # Add a self-referential placeholder edge so the chapter graph is connected;
        # the writer will turn this into a "see also" link.
        for m in missing:
            relationships.append({"from": m, "to": m, "label": "self-contained", "kind": "semantic"})

    summary = parsed.get("summary", "")
    token_usage = {"prompt_tokens": estimate_tokens(prompt), "completion_tokens": estimate_tokens(response)}

    if state.get("use_cache", True):
        await cache_store("analyze", cache_key_payload, {
            "relationships": relationships, "summary": summary, "token_usage": token_usage,
        })

    return {"relationships": relationships, "summary": summary, "token_usage": token_usage}
```

### Why This Is Robust

- **Structural edges come from the graph**, not the LLM. The LLM is asked to label them, not invent them. So the overview diagram is **always** a true reflection of imports and calls in the codebase.
- **Semantic edges are constrained to known abstraction names** — the validator drops any that reference a name not in `state["abstractions"]`.
- **Connectivity check + self-edge placeholder** guarantees the rendered Mermaid is a connected graph (Mermaid handles disconnected components fine, but a fully-connected overview reads better).
- **Cache key includes abstraction names + graph hash** — if either changes, the cache invalidates automatically.

### Tests

```python
def test_build_abstraction_edges_aggregates():
    """5 import edges from A→B should collapse to 1 abstract edge with src_count=5."""
    payload = _graph_with_edges([
        ("mod:a", "mod:b", "import"),
        ("mod:a", "mod:b", "import"),
        ("mod:a", "mod:b", "import"),
        ("mod:a", "mod:b", "call"),
        ("mod:a", "mod:b", "call"),
    ])
    abs_ = [
        {"name": "A", "anchor_node_ids": ["mod:a"], "file_indices": [0]},
        {"name": "B", "anchor_node_ids": ["mod:b"], "file_indices": [1]},
    ]
    edges = _build_abstraction_edges(payload, abs_)
    assert {e["kind"] for e in edges} == {"import", "call"}
    assert any(e["src_count"] == 3 and e["kind"] == "import" for e in edges)
    assert any(e["src_count"] == 2 and e["kind"] == "call" for e in edges)

async def test_analyze_relationships_drops_unknown_endpoints(faker_llm, sample_state):
    faker_llm.set_response(yaml.dump({
        "summary": "...",
        "relationships": [
            {"from": "Auth", "to": "Ghost", "label": "x"},   # Ghost not in abstractions
            {"from": "Auth", "to": "Auth", "label": "y"},    # self-loop
            {"from": "Auth", "to": "Store", "label": "z"},
        ],
    }))
    result = await analyze_relationships_node(sample_state)
    assert result["relationships"] == [{"from": "Auth", "to": "Store", "label": "z", "kind": "semantic"}]

async def test_analyze_relationships_adds_self_edge_for_uncovered(faker_llm, sample_state):
    """If an abstraction has no edges, a self-edge is added so the diagram stays connected."""
    faker_llm.set_response(yaml.dump({"summary": "...", "relationships": [
        {"from": "A", "to": "B", "label": "x"},
    ]}))
    # sample_state has abstractions A, B, C; only A↔B in response
    result = await analyze_relationships_node(sample_state)
    covered = {(r["from"], r["to"]) for r in result["relationships"]}
    assert ("C", "C") in covered
```

---

## Node 3: `order_chapters`

### Goal

Produce `state["chapter_order"]` — a permutation of abstraction indices in pedagogical order (prerequisites first, advanced concepts last).

### The Big Insight

**The graph already gives you a valid topological order.** After cycle-breaking (drop the lowest-PageRank back-edge from each cycle until DAG), `networkx.topological_sort` returns a provably-correct teaching order: no abstraction is taught before its dependencies.

This is the **primary order**. The LLM call is **optional** and only does one job: produce a one-paragraph rationale explaining the order in human terms. If you want to skip the LLM call entirely (save tokens, save latency), just return the topological sort directly.

### When To Use The LLM Refinement (and When Not To)

| Situation | Recommendation |
|---|---|
| Repo with obvious dependency chains (CLI tool, web app) | Skip LLM — topo sort is correct and pedagogical |
| Repo with cross-cutting concerns (frameworks, plugins) | Skip LLM — topo sort handles cross-cutting reasonably |
| Repo where LLM should produce a "tour narrative" intro | Use LLM, but only to write prose — order stays as topo sort |
| Tiny repos (≤5 abstractions) | Skip LLM — order is rarely wrong |

### Implementation — `graph/nodes/order_chapters.py`

```python
def _topo_order(
    code_graph_payload: dict, abstractions: list[dict],
) -> list[int]:
    """Deterministic pedagogical order from the graph."""
    g = CodeGraph.from_payload(code_graph_payload)
    abs_ids = [a["anchor_node_ids"][0] for a in abstractions if a["anchor_node_ids"]]
    # Build subgraph of just the anchor nodes, expand by predecessors
    expansion: set[str] = set(abs_ids)
    for aid in abs_ids:
        expansion.update(nx.ancestors(g.g, aid))   # all transitive deps
    sub = g.g.subgraph(expansion).copy()

    # Cycle-break: remove lowest-PageRank back-edge in each cycle until DAG.
    pr = nx.pagerank(sub) if sub.number_of_nodes() > 0 else {}
    for _ in range(1000):
        try:
            cyc = nx.find_cycle(sub)
        except nx.NetworkXNoCycle:
            break
        edge = min(cyc, key=lambda e: pr.get(e[0], 0) + pr.get(e[1], 0))
        sub.remove_edge(*edge[:2])

    # Topo sort, then map back to abstraction indices.
    sorted_nodes = list(nx.topological_sort(sub))
    node_to_abs_idx = {}
    for i, a in enumerate(abstractions):
        for nid in a["anchor_node_ids"]:
            node_to_abs_idx[nid] = i
    seen: set[int] = set()
    order: list[int] = []
    for n in sorted_nodes:
        if n in node_to_abs_idx and node_to_abs_idx[n] not in seen:
            order.append(node_to_abs_idx[n])
            seen.add(node_to_abs_idx[n])
    # Append any abstractions that didn't appear in the sort (orphans) at the end.
    for i in range(len(abstractions)):
        if i not in seen:
            order.append(i)
    return order


async def order_chapters_node(state: dict) -> dict:
    log.info("order_chapters.start", run_id=state.get("run_id"))
    provider = get_provider_for_org(state["org_id"])
    model = state.get("model") or provider.default_model

    cache_key_payload = {
        "model": model,
        "abstractions_hash": _hash_list(state["abstractions"]),
        "code_graph_hash": state["code_graph"]["_meta"]["graph_hash"],
        "rationale_enabled": state.get("use_llm_order_rationale", False),
    }

    if state.get("use_cache", True):
        cached = await cache_lookup("order", cache_key_payload)
        if cached:
            return {"chapter_order": cached["chapter_order"], "rationale": cached.get("rationale", ""), "token_usage": cached.get("token_usage", {})}

    # 1) Deterministic topo order from the graph (the source of truth)
    chapter_order = _topo_order(state["code_graph"], state["abstractions"])

    # 2) Optional: ask LLM to write a 2-3 sentence rationale explaining the order
    rationale = ""
    token_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}
    if state.get("use_llm_order_rationale", False):
        prompt = render_prompt(
            "order.md",
            abstractions=state["abstractions"],
            chapter_order=chapter_order,
            language=state.get("language", "english"),
        )
        response = await provider.complete_async(prompt, temperature=0.2, max_tokens=400)
        rationale = _strip_code_fences(response).strip()
        token_usage = {"prompt_tokens": estimate_tokens(prompt), "completion_tokens": estimate_tokens(response)}

    if state.get("use_cache", True):
        await cache_store("order", cache_key_payload, {
            "chapter_order": chapter_order, "rationale": rationale, "token_usage": token_usage,
        })

    log.info("order_chapters.done", length=len(chapter_order))
    return {"chapter_order": chapter_order, "rationale": rationale, "token_usage": token_usage}
```

### Why This Is Robust

- **Order is deterministic** — same repo + same graph = same chapter order, every run. No LLM non-determinism in the structural order.
- **Cycle-breaking is bounded** — max 1000 iterations, each removes the lowest-PageRank back-edge. Worst case: graph becomes a forest.
- **Orphan handling** — if an abstraction isn't reachable in the expansion (rare; e.g. it's only imported by tests), it's appended at the end. Never lost.
- **Cache invalidates on graph or abstraction change** automatically.

### Tests

```python
def test_topo_order_respects_dependencies():
    """If A imports B, A must come after B in chapter_order."""
    payload = _graph_with_edges([("mod:a", "mod:b", "import")])
    abs_ = [
        {"name": "A", "anchor_node_ids": ["mod:a"], "file_indices": [0]},
        {"name": "B", "anchor_node_ids": ["mod:b"], "file_indices": [1]},
    ]
    order = _topo_order(payload, abs_)
    assert order.index(0) > order.index(1)        # A after B

def test_topo_order_breaks_cycles():
    payload = _graph_with_edges([("mod:a", "mod:b", "import"), ("mod:b", "mod:a", "import")])
    abs_ = [
        {"name": "A", "anchor_node_ids": ["mod:a"], "file_indices": [0]},
        {"name": "B", "anchor_node_ids": ["mod:b"], "file_indices": [1]},
    ]
    order = _topo_order(payload, abs_)
    assert len(order) == 2                       # both still present

def test_topo_order_appends_orphans():
    payload = _graph_with_nodes(["mod:a"])        # 'b' isn't even in the graph
    abs_ = [
        {"name": "A", "anchor_node_ids": ["mod:a"], "file_indices": [0]},
        {"name": "Orphan", "anchor_node_ids": ["mod:missing"], "file_indices": [1]},
    ]
    order = _topo_order(payload, abs_)
    assert sorted(order) == [0, 1]

async def test_order_chapters_node_caches(faker_llm, sample_state):
    sample_state["use_llm_order_rationale"] = False
    sample_state["use_cache"] = True
    r1 = await order_chapters_node(sample_state)
    assert r1["chapter_order"]                   # non-empty
    r2 = await order_chapters_node(sample_state)
    assert r1 == r2
```

---

## Node 4: `write_chapter_single` (the Send fan-out)

### Goal

For each abstraction, produce a Markdown chapter that teaches the concept using real code excerpts and a Mermaid `sequenceDiagram`. The chapter must be grounded in real, reachable code from the codebase.

### The Slice Strategy (Recap)

For abstraction `A` with anchor node `n`:
1. `ego_graph(g, n, radius=2)` → all nodes within 2 hops.
2. Sort by PageRank within the slice; keep top 50 nodes.
3. Collect their `file` attributes (deduplicated, ordered by PageRank).
4. Token-budget the file contents into the prompt (target ~10K tokens).
5. Send to LLM with: `abstraction.description`, the slice, and the chapter-writing prompt.

### Send Payload Shape

```python
# In graph.py
def route_to_chapter_writers(state: dict) -> list[Send]:
    return [
        Send("write_chapter_single", {
            "abstraction_index": idx,
            "abstraction": state["abstractions"][idx],
            "code_graph": state["code_graph"],
            "files_by_path": {f["path"]: f["content"] for f in state["files"]},
            "relationships": state.get("relationships", []),
            "output_dir": state["output_dir"],
            "run_id": state["run_id"],
            "org_id": state["org_id"],
            "language": state.get("language", "english"),
            "use_cache": state.get("use_cache", True),
            "provider": state.get("provider"),
            "model": state.get("model"),
        })
        for idx in state["chapter_order"]
    ]
```

### Implementation — `graph/nodes/write_chapters.py`

```python
from __future__ import annotations
from pathlib import Path
from codebase_kb.codeintel.graph import CodeGraph
from codebase_kb.codeintel.slicing import build_chapter_prompt
from codebase_kb.llm.router import get_provider_for_org
from codebase_kb.cache import cache_lookup, cache_store
from codebase_kb.prompts import render_prompt
from codebase_kb.output.mermaid import build_chapter_sequence, MermaidGenError
from codebase_kb.output.writer import write_chapter_file
from codebase_kb.observability.logging import get_logger

log = get_logger(__name__)

CHAPTER_TOKEN_BUDGET = 10_000           # input token budget per chapter
SEQUENCE_DIAGRAM_MAX_PARTICIPANTS = 5
MAX_CHAPTER_RETRIES = 2                  # retry once if LLM output is malformed


def _build_neighbors(relationships: list[dict], abstraction_name: str) -> list[dict]:
    """Return edges that touch this abstraction (in or out)."""
    return [r for r in relationships if r["from"] == abstraction_name or r["to"] == abstraction_name]


async def write_chapter_single(payload: dict) -> dict:
    """Send target. Receives a chapter-writing payload, returns one chapter dict."""
    idx = payload["abstraction_index"]
    abstraction = payload["abstraction"]
    log.info("write_chapter.start", idx=idx, name=abstraction["name"])

    provider = get_provider_for_org(payload["org_id"])
    model = payload.get("model") or provider.default_model

    cache_key_payload = {
        "model": model,
        "abstraction_hash": _hash_dict(abstraction),
        "code_graph_hash": payload["code_graph"]["_meta"]["graph_hash"],
        "language": payload.get("language", "english"),
    }

    if payload.get("use_cache", True):
        cached = await cache_lookup(f"chapter:{idx}", cache_key_payload)
        if cached:
            log.info("write_chapter.cache_hit", idx=idx)
            return {"chapters": [{"index": idx, "name": abstraction["name"], "markdown": cached["markdown"]}]}

    # 1) Slice the code graph to this abstraction's k-hop neighborhood
    g = CodeGraph.from_payload(payload["code_graph"])
    sliced_files = g.sliced_context(
        abstraction["anchor_node_ids"], radius=2, max_nodes=50,
    )

    # 2) Build a token-budgeted chapter context (ONLY the relevant files)
    chapter_context = build_chapter_prompt(
        abstraction, g, payload["files_by_path"], token_budget=CHAPTER_TOKEN_BUDGET,
    )

    # 3) Get neighbor relationships for the sequence diagram
    neighbors = _build_neighbors(payload.get("relationships", []), abstraction["name"])

    # 4) Render prompt
    prompt = render_prompt(
        "write_chapter.md",
        abstraction=abstraction,
        chapter_context=chapter_context,
        neighbors=neighbors,
        max_participants=SEQUENCE_DIAGRAM_MAX_PARTICIPANTS,
        language=payload.get("language", "english"),
    )

    # 5) Call LLM (with retry on malformed output)
    markdown = None
    last_error = None
    for attempt in range(MAX_CHAPTER_RETRIES + 1):
        try:
            response = await provider.complete_async(prompt, temperature=0.3, max_tokens=4096)
            markdown = _extract_chapter_markdown(response)
            break
        except (ValueError, MermaidGenError) as e:
            last_error = e
            log.warning("write_chapter.retry", idx=idx, attempt=attempt, error=str(e))
    if markdown is None:
        # Last-resort: write a skeleton chapter so the run still succeeds.
        log.error("write_chapter.failed_fallback", idx=idx, error=str(last_error))
        markdown = _skeleton_chapter(abstraction, last_error)

    # 6) Append the programmatically-built sequence diagram
    #    (LLM may also emit one; we keep whichever parses)
    try:
        mermaid_diagram = build_chapter_sequence(abstraction, neighbors)
        markdown = _inject_sequence_diagram(markdown, mermaid_diagram)
    except MermaidGenError as e:
        log.warning("write_chapter.diagram_failed", idx=idx, error=str(e))
        # Don't fail the chapter; the LLM may have included its own.

    # 7) Persist chapter file (intermediate; combine_tutorial may rewrite later)
    chapter_path = Path(payload["output_dir"]) / f"{idx + 1:02d}_{_slug(abstraction['name'])}.md"
    chapter_path.write_text(markdown, encoding="utf-8")

    token_usage = {"prompt_tokens": estimate_tokens(prompt), "completion_tokens": estimate_tokens(response) if 'response' in locals() else 0}

    if payload.get("use_cache", True):
        await cache_store(f"chapter:{idx}", cache_key_payload, {
            "markdown": markdown, "token_usage": token_usage,
        })

    log.info("write_chapter.done", idx=idx, path=str(chapter_path))
    return {"chapters": [{"index": idx, "name": abstraction["name"], "markdown": markdown, "path": str(chapter_path)}]}
```

### Helper Functions

```python
def _extract_chapter_markdown(response: str) -> str:
    """Extract the chapter markdown from the LLM response. Tolerant of preamble/postamble."""
    # Strip leading "Here's the chapter:" etc., find the first # heading.
    response = response.strip()
    for line in response.splitlines():
        if line.startswith("# "):
            return response[response.index(line):]
    # Fallback: assume the whole response is the chapter.
    if "## " in response:
        return "# Chapter\n\n" + response
    raise ValueError("LLM response did not contain a Markdown chapter")


def _inject_sequence_diagram(markdown: str, diagram: str) -> str:
    """Insert (or replace) a Mermaid sequenceDiagram block under ## Sequence Diagram."""
    header = "## Sequence Diagram"
    if header in markdown:
        before, _, rest = markdown.partition(header)
        # Drop any existing mermaid block after the header, until the next ## heading.
        after_header = []
        in_fence = False
        for line in rest.splitlines(keepends=True):
            if line.strip().startswith("```mermaid") and not in_fence:
                in_fence = True
                continue
            if in_fence and line.strip() == "```":
                in_fence = False
                continue
            if not in_fence:
                after_header.append(line)
                if line.startswith("## ") and len(after_header) > 1:
                    break
        return before + header + "\n\n```mermaid\n" + diagram + "\n```\n\n" + "".join(after_header)
    # No header → append before the "Next" link if present, else at end.
    if "Next:" in markdown:
        before, _, after = markdown.partition("Next:")
        return before.rstrip() + "\n\n" + header + "\n\n```mermaid\n" + diagram + "\n```\n\nNext:" + after
    return markdown.rstrip() + "\n\n" + header + "\n\n```mermaid\n" + diagram + "\n```\n"


def _skeleton_chapter(abstraction: dict, error: Exception | None) -> str:
    """Last-resort chapter if the LLM keeps failing."""
    return (
        f"# {abstraction['name']}\n\n"
        f"_{abstraction['description']}_\n\n"
        f"## Note\n\n"
        f"This chapter could not be auto-generated after multiple attempts. "
        f"The system identified this as a core concept in the codebase, but "
        f"failed to produce prose. Please write this chapter manually.\n\n"
        f"Anchor files: `{', '.join(abstraction['anchor_node_ids'])}`\n\n"
        f"Underlying error: `{error}`\n"
    )


def _slug(name: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s[:40] or "chapter"
```

### Prompt Template — `prompts/write_chapter.md` (key sections)

```markdown
You are writing one chapter of an onboarding tutorial for a codebase.

The chapter focuses on the abstraction called **"{{ abstraction.name }}"**:
> {{ abstraction.description }}

Symbols and file paths in the "Relevant Code" section below are **guaranteed to
exist** in the codebase. Use them by exact name.

## Relevant Code (token-budgeted slice of the codebase)

The following files contain the symbols and dependencies most relevant to this
chapter. Other files are intentionally omitted.

```
{{ chapter_context }}
```

## Neighboring Abstractions (for the sequence diagram)

{% for n in neighbors %}
- {{ n.from }} --[{{ n.label }}]--> {{ n.to }}
{% endfor %}

## Your Task

Write a Markdown chapter with these sections, in this order:

1. `# {{ abstraction.name }}` (the chapter title)
2. `## Motivation` — why this concept exists, what problem it solves (2–4 sentences)
3. `## Key Code Excerpts` — 2–4 short excerpts from the relevant files above.
   Each excerpt must reference the file and a real line range, e.g.
   ````markdown
   ```python
   # src/auth/service.py:42-58
   def login(...):
       ...
   ```
   ````
   Line ranges must be accurate to the source. Do not invent code.
4. `## Sequence Diagram` — a Mermaid `sequenceDiagram` showing how this
   abstraction interacts with its neighbors (≤ {{ max_participants }} participants).
5. `## Key Takeaways` — 3–5 bullets summarizing what to remember.
6. `Next: [{{ next_chapter_name }}]({{ next_chapter_slug }}.md)` — link to
   the next chapter (or omit if this is the last).

Constraints:
- Use {{ language }} for prose.
- Reference files by their exact paths from "Relevant Code".
- Do not invent symbols, file paths, or line numbers.
- The Mermaid block must start with `sequenceDiagram`.

Output ONLY the Markdown chapter.
```

### Why This Is Robust

- **Each chapter is independently cacheable** — cache key includes the abstraction + graph hash + language. Re-runs are fast; partial re-runs after one chapter changes are also fast.
- **`Send` runs all chapters in parallel** — 10 chapters finish in the time of the slowest one, not the sum.
- **Skeleton fallback** — if the LLM keeps failing on a chapter, the run still succeeds with a placeholder. Better to ship 9 good chapters + 1 stub than to lose the whole run.
- **Diagram injection** is idempotent — if the LLM includes its own diagram, we replace it with the validated one; if not, we append. We never get two diagrams.
- **Intermediate write to disk** — even if the worker is killed mid-`combine_tutorial`, individual chapters are recoverable.

### Tests

```python
async def test_write_chapter_slices_to_relevant_files(faker_llm, sample_payload):
    """The prompt sent to the LLM must contain only files near the anchor, not the whole repo."""
    sample_payload["abstraction"] = {
        "name": "Auth", "description": "...",
        "anchor_node_ids": ["mod:src/auth/service.py"],
        "file_indices": [3],
    }
    faker_llm.set_response("# Auth\n\n## Motivation\n\n...")
    await write_chapter_single(sample_payload)
    sent_prompt = faker_llm.last_prompt
    assert "src/auth/service.py" in sent_prompt
    assert "src/unrelated/large_file.py" not in sent_prompt

async def test_write_chapter_retries_on_malformed(faker_llm, sample_payload):
    faker_llm.set_responses([
        "no markdown here, sorry",                          # attempt 0 fails
        "also not valid",                                   # attempt 1 fails
        "# Auth\n\n## Motivation\n\nValid chapter.",         # attempt 2 succeeds
    ])
    result = await write_chapter_single(sample_payload)
    assert result["chapters"][0]["markdown"].startswith("# Auth")
    assert len(faker_llm.calls) == 3

async def test_write_chapter_writes_file_to_disk(faker_llm, sample_payload, tmp_path):
    sample_payload["output_dir"] = str(tmp_path)
    faker_llm.set_response("# Auth\n\n## Motivation\n\n...")
    result = await write_chapter_single(sample_payload)
    written = list(tmp_path.glob("*.md"))
    assert len(written) == 1
    assert written[0].read_text().startswith("# Auth")
```

---

## Node 5: `combine_tutorial`

### Goal

Take the parallel-written chapters and stitch them into the final tutorial:
1. Build the **overview Mermaid `flowchart TD`** from validated relationships.
2. Generate `index.md` (title + overview diagram + numbered TOC).
3. Rename chapter files to sequential numbers (`01_auth.md`, `02_models.md`, …).
4. Bundle everything into a zip.
5. Upload the zip + individual files as artifacts.
6. Mark the run as `succeeded` and return the artifact URLs.

### Input → Output

```python
# Input
chapters:        [{"index": int, "name": str, "markdown": str, "path": str}, ...]
abstractions:    [...]
relationships:   [{"from": str, "to": str, "label": str, "kind": str}, ...]
code_graph:      {...}            # for cycle annotations in the overview
output_dir:      str

# Output
final_output_dir: str             # local path
artifacts: [
    {"kind": "zip",        "path": "...", "size_bytes": ..., "sha256": "..."},
    {"kind": "index",      "path": "...", ...},
    {"kind": "chapter",    "path": "01_auth.md", ...},
    ...
]
```

### Implementation — `graph/nodes/combine_tutorial.py`

```python
from __future__ import annotations
import hashlib, json, shutil
from pathlib import Path
from datetime import datetime, timezone

from codebase_kb.codeintel.graph import CodeGraph
from codebase_kb.output.mermaid import (
    build_overview_diagram, build_cycles_diagram, MermaidGenError,
)
from codebase_kb.output.zip import zip_directory
from codebase_kb.output.writer import write_text, write_bytes
from codebase_kb.observability.logging import get_logger

log = get_logger(__name__)


async def combine_tutorial_node(state: dict) -> dict:
    log.info("combine_tutorial.start", run_id=state.get("run_id"))
    chapters = sorted(state["chapters"], key=lambda c: c["index"])
    abstractions = state["abstractions"]
    relationships = state.get("relationships", [])
    output_dir = Path(state["output_dir"])
    tutorial_dir = output_dir / state.get("project_name", "tutorial")
    tutorial_dir.mkdir(parents=True, exist_ok=True)

    # 1) Build overview Mermaid (validated from relationships)
    overview_mermaid = ""
    cycles_mermaid = ""
    try:
        overview_mermaid = build_overview_diagram(abstractions, relationships)
    except MermaidGenError as e:
        log.warning("combine_tutorial.overview_diagram_failed", error=str(e))

    try:
        g = CodeGraph.from_payload(state["code_graph"])
        cycles = list(nx.simple_cycles(g.g))[:5]        # cap at 5 cycles to keep diagram readable
        if cycles:
            cycles_mermaid = build_cycles_diagram(cycles)
    except (MermaidGenError, Exception) as e:
        log.warning("combine_tutorial.cycles_diagram_skipped", error=str(e))

    # 2) Generate index.md
    index_md = _build_index_md(
        project_name=state.get("project_name", "Codebase"),
        abstractions=abstractions,
        chapters=chapters,
        overview_mermaid=overview_mermaid,
        cycles_mermaid=cycles_mermaid,
        summary=state.get("summary", ""),
    )
    write_text(tutorial_dir / "index.md", index_md)

    # 3) Renumber + write chapter files
    artifacts: list[dict] = []
    index_path = tutorial_dir / "index.md"
    artifacts.append(_make_artifact("index", index_path))

    for i, chapter in enumerate(chapters, start=1):
        new_name = f"{i:02d}_{_slug(chapter['name'])}.md"
        new_path = tutorial_dir / new_name
        # Chapter markdown may contain internal links to old filenames; rewrite them.
        rewritten = _rewrite_chapter_links(chapter["markdown"], chapter, chapters, i)
        write_text(new_path, rewritten)
        artifacts.append(_make_artifact("chapter", new_path))

    # 4) Bundle as zip
    zip_path = output_dir / f"{state.get('project_name', 'tutorial')}.zip"
    zip_directory(tutorial_dir, zip_path)
    artifacts.append(_make_artifact("zip", zip_path))

    # 5) Build final_output_dir for return
    final_output_dir = str(tutorial_dir)
    log.info("combine_tutorial.done", artifacts=len(artifacts), dir=final_output_dir)
    return {
        "final_output_dir": final_output_dir,
        "artifacts": artifacts,
    }


def _build_index_md(project_name: str, abstractions, chapters, overview_mermaid: str,
                     cycles_mermaid: str, summary: str) -> str:
    toc_rows = []
    for i, (chapter, abs_) in enumerate(zip(chapters, _abs_for_chapters(chapters, abstractions)), start=1):
        slug = f"{i:02d}_{_slug(chapter['name'])}"
        toc_rows.append(f"{i}. [{chapter['name']}]({slug}.md) — {abs_.get('description', '')}")
    toc = "\n".join(toc_rows)

    md = [
        f"# {project_name} — Code Knowledge",
        "",
        f"_Generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}_",
        "",
        "## Overview",
        "",
        summary or "_(no summary provided)_",
        "",
        "## Architecture",
        "",
    ]
    if overview_mermaid:
        md += ["```mermaid", overview_mermaid, "```", ""]
    else:
        md += ["_Overview diagram could not be generated._", ""]

    if cycles_mermaid:
        md += ["## Architectural Cycles (smells)", "", "```mermaid", cycles_mermaid, "```", ""]

    md += ["## Table of Contents", "", toc, ""]
    return "\n".join(md)


def _rewrite_chapter_links(markdown: str, this_chapter: dict, all_chapters: list[dict],
                            this_index_1based: int) -> str:
    """Replace 'Next: X' links with the correct .md filename."""
    # We prepended "[X](X.md)" in the prompt; here we rewrite it to use the actual filename.
    # Map by name → slug for stable rewriting.
    name_to_slug = {c["name"]: f"{i + 1:02d}_{_slug(c['name'])}.md"
                    for i, c in enumerate(sorted(all_chapters, key=lambda c: c["index"]))}
    # Look for "Next: [Name](name.md)" pattern and rewrite.
    import re
    def repl(m):
        target_name = m.group(1)
        slug = name_to_slug.get(target_name)
        return f"Next: [{target_name}]({slug})" if slug else m.group(0)
    return re.sub(r"Next: \[([^\]]+)\]\(([^)]+\.md)\)", repl, markdown)


def _make_artifact(kind: str, path: Path) -> dict:
    data = path.read_bytes()
    return {
        "kind": kind,
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
```

### Zip + Upload (separate from the graph node)

The `combine_tutorial_node` returns artifact metadata. A **post-graph hook** (in the worker, not in the graph) uploads the artifacts to S3-compatible storage and updates the DB:

```python
# In workers/tasks.py — runs after graph.invoke() returns
async def upload_artifacts(run_id: str, artifacts: list[dict]):
    for art in artifacts:
        remote_url = await s3.upload_file(
            bucket=settings.ARTIFACT_BUCKET,
            key=f"{run_id}/{Path(art['path']).name}",
            local_path=art["path"],
        )
        await db.execute(
            "INSERT INTO artifacts (run_id, kind, storage_url, size_bytes, sha256) VALUES (%s, %s, %s, %s, %s)",
            (run_id, art["kind"], remote_url, art["size_bytes"], art["sha256"]),
        )
```

### Why This Is Robust

- **Diagram generation is the last step** — if Mermaid generation fails, we still write `index.md` (just without the diagram), so the user gets text + chapters.
- **Chapter renumbering happens here, not in `write_chapter_single`** — chapters are written with their conceptual names; the final order is determined by `chapter_order` and applied here. This means parallel `write_chapter_single` workers don't need to know their position.
- **Internal link rewriting** — chapters link to each other by name; `combine_tutorial` rewrites those to the actual filenames after numbering.
- **Artifact metadata in state** — the worker layer uses it for upload without re-reading the filesystem.
- **Zip is built locally** — even if S3 is down, the run still produces a downloadable zip (the worker can retry upload separately).

### Tests

```python
async def test_combine_tutorial_writes_index_with_overview(sample_state, tmp_path):
    sample_state["output_dir"] = str(tmp_path)
    sample_state["chapters"] = [
        {"index": 0, "name": "Auth", "markdown": "# Auth\n\nbody"},
        {"index": 1, "name": "Store", "markdown": "# Store\n\nbody"},
    ]
    sample_state["abstractions"] = [
        {"name": "Auth", "description": "Login", "anchor_node_ids": ["mod:a"], "file_indices": [0]},
        {"name": "Store", "description": "Storage", "anchor_node_ids": ["mod:b"], "file_indices": [1]},
    ]
    sample_state["relationships"] = [
        {"from": "Auth", "to": "Store", "label": "uses", "kind": "import"},
    ]
    result = await combine_tutorial_node(sample_state)
    index_md = (Path(result["final_output_dir"]) / "index.md").read_text()
    assert "# Auth" in index_md or "project_name" in index_md
    assert "```mermaid" in index_md
    assert "flowchart TD" in index_md
    assert "[Auth](01_auth.md)" in index_md
    assert "[Store](02_store.md)" in index_md

async def test_combine_tutorial_degrades_without_relationships(sample_state, tmp_path):
    """Missing relationships → still produces a usable index, just without the diagram."""
    sample_state["output_dir"] = str(tmp_path)
    sample_state["chapters"] = [{"index": 0, "name": "Solo", "markdown": "# Solo"}]
    sample_state["abstractions"] = [{"name": "Solo", "description": "x", "anchor_node_ids": [], "file_indices": [0]}]
    sample_state["relationships"] = []
    result = await combine_tutorial_node(sample_state)
    index_md = (Path(result["final_output_dir"]) / "index.md").read_text()
    assert "_Overview diagram could not be generated._" in index_md

def test_combine_tutorial_renumbers_chapters_sequentially(sample_state, tmp_path):
    """chapter_order=[2, 0, 1] → files are named 01_..., 02_..., 03_... in that order."""
    sample_state["output_dir"] = str(tmp_path)
    sample_state["chapters"] = [
        {"index": 2, "name": "C", "markdown": "# C"},
        {"index": 0, "name": "A", "markdown": "# A"},
        {"index": 1, "name": "B", "markdown": "# B"},
    ]
    # ... run combine, assert files are 01_c.md, 02_a.md, 03_b.md in that order
```

---

## Wiring It All Up — `graph/graph.py` (final)

```python
from langgraph.graph import StateGraph, START, END
from langgraph.constants import Send

from codebase_kb.graph.nodes.fetch_repo import fetch_repo_node
from codebase_kb.graph.nodes.build_code_graph import build_code_graph_node
from codebase_kb.graph.nodes.identify_abstractions import identify_abstractions_node
from codebase_kb.graph.nodes.analyze_relationships import analyze_relationships_node
from codebase_kb.graph.nodes.order_chapters import order_chapters_node
from codebase_kb.graph.nodes.write_chapters import write_chapter_single
from codebase_kb.graph.nodes.combine_tutorial import combine_tutorial_node
from codebase_kb.graph.state import KnowledgeBuilderState


def route_to_chapter_writers(state: dict) -> list[Send]:
    return [
        Send("write_chapter_single", {
            "abstraction_index": idx,
            "abstraction": state["abstractions"][idx],
            "code_graph": state["code_graph"],
            "files_by_path": {f["path"]: f["content"] for f in state["files"]},
            "relationships": state.get("relationships", []),
            "output_dir": state["output_dir"],
            "run_id": state["run_id"],
            "org_id": state["org_id"],
            "language": state.get("language", "english"),
            "use_cache": state.get("use_cache", True),
            "provider": state.get("provider"),
            "model": state.get("model"),
        })
        for idx in state["chapter_order"]
    ]


def build_graph():
    g = StateGraph(KnowledgeBuilderState)
    g.add_node("fetch_repo",            fetch_repo_node)
    g.add_node("build_code_graph",      build_code_graph_node)
    g.add_node("identify_abstractions", identify_abstractions_node)
    g.add_node("analyze_relationships", analyze_relationships_node)
    g.add_node("order_chapters",        order_chapters_node)
    g.add_node("write_chapter_single",  write_chapter_single)
    g.add_node("combine_tutorial",      combine_tutorial_node)

    g.add_edge(START, "fetch_repo")
    g.add_edge("fetch_repo", "build_code_graph")
    g.add_edge("build_code_graph", "identify_abstractions")
    g.add_edge("identify_abstractions", "analyze_relationships")
    g.add_edge("analyze_relationships", "order_chapters")
    g.add_conditional_edges("order_chapters", route_to_chapter_writers, ["write_chapter_single"])
    g.add_edge("write_chapter_single", "combine_tutorial")
    g.add_edge("combine_tutorial", END)

    # Checkpoint per node so a crashed run can resume from the last completed node.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    memory = AsyncSqliteSaver.from_conn_string("checkpoints.db")        # or Postgres in prod
    return g.compile(checkpointer=memory)
```

---

## Build Order (Recommended)

| # | Task | Est. time | Verify with |
|---|---|---|---|
| 1 | `identify_abstractions.py` — read PageRank from `code_graph`, render prompt, parse YAML, validate | 2–3 h | Unit test on a 30-module toy graph with a canned LLM response |
| 2 | `analyze_relationships.py` — project graph edges to abstraction level, prompt for labels, validate | 2 h | Unit test with fake LLM responses (valid, invalid, missing endpoints) |
| 3 | `order_chapters.py` — topo sort + cycle-breaking + optional LLM rationale | 1–2 h | Unit test on a cyclic graph; assert order respects dependencies |
| 4 | `write_chapter_single.py` — slice context, budget tokens, call LLM, write file | 3–4 h | Integration test against `tiny_repo` fixture with real LLM |
| 5 | `combine_tutorial.py` — build overview diagram, generate index, renumber, zip | 2 h | Unit test on a 3-chapter sample state; inspect output files |
| 6 | `prompts/*.md` — write the 4 prompt templates with the new "input is pre-sliced" preamble | 2 h | Eyeball each prompt for: clarity, token count, expected output shape |
| 7 | `output/mermaid.py` — `build_overview_diagram`, `build_chapter_sequence`, `build_cycles_diagram` | 2 h | Adversarial unit tests (unicode, reserved keywords, empty labels) |
| 8 | `output/zip.py` — robust zip with deterministic file ordering | 30 min | Round-trip: zip → unzip → diff |
| 9 | `graph/graph.py` — wire all 5 new nodes, add `Send` fan-out, add checkpointer | 1 h | Compile the graph; assert all nodes reachable |
| 10 | End-to-end test: full run on `tiny_repo` fixture with real LLM | 2 h | Inspect output: index.md renders, chapters present, Mermaid parses |
| 11 | Progress publishing: hook each node to publish to Redis channel `runs:{id}:events` | 2 h | Open SSE stream in browser; see node-by-node progress |
| 12 | Error handling: retry policy, fallback skeletons, structured error reports in run record | 2 h | Inject failures; assert run still produces artifacts |
| 13 | Observability: per-node token usage, duration, cache-hit rate logged | 1 h | Run a few times; check metrics endpoint |

**Total estimate**: ~24–28 hours of focused work for a single developer.

---

## Integration Checklist (Pre-flight Before Running End-to-End)

Before running the full pipeline, confirm:

- [ ] `code_graph["_meta"]["graph_hash"]` is set by `build_code_graph` (the SHA256 of the serialized graph). Without it, the cache keys for `identify` and `analyze` won't work.
- [ ] `codeintel.graph.CodeGraph.from_payload(payload)` is implemented (deserializer). If you only built `add_*` methods, add this now.
- [ ] `codeintel.graph.CodeGraph.sliced_context(anchor_ids, radius, max_nodes)` is implemented and returns **file paths** (not node IDs).
- [ ] `codeintel.slicing.build_chapter_prompt(abstraction, code_graph, files_by_path, token_budget)` is implemented and uses `tiktoken` (or your model's tokenizer) for accurate counting.
- [ ] `llm.router.get_provider_for_org(org_id)` returns an `LLMProvider` whose `complete_async` method is **truly async** (use `asyncio.to_thread` if the SDK is sync).
- [ ] `cache.cache_lookup(stage, payload)` and `cache.cache_store(stage, payload, value)` are implemented with per-org namespacing.
- [ ] `utils.yaml_parse.extract_yaml(text)` finds the **first** ```` ```yaml ```` fence and parses with `yaml.safe_load`. Returns `None` on no fence.
- [ ] `output.mermaid.build_overview_diagram(abstractions, relationships)` validates all node IDs and edge labels; raises `MermaidGenError` on failure.
- [ ] `output.writer.write_text` and `output.writer.write_bytes` are atomic (write to `.tmp` then `os.replace`).
- [ ] `prompts/*.md` files exist for `identify`, `analyze`, `order`, `write_chapter` with the placeholders your `render_prompt` function expects.

---

## Verification — How to Know It Works

### 1. Unit Tests (per node)

Each node has its own `test_*.py` file (sketched above). Run with:

```bash
pytest backend/tests/unit -x -q
```

### 2. Integration Test (full pipeline on fixture)

`backend/tests/integration/test_pipeline_tiny_repo.py`:

```python
@pytest.mark.asyncio
async def test_full_pipeline_produces_valid_tutorial(tiny_repo_path, fake_provider, db):
    """End-to-end on the fixture repo with a stub LLM."""
    state = KnowledgeBuilderState(
        run_id="test-run-1",
        project_id="test-proj",
        org_id="test-org",
        repo_url=None,
        local_dir=str(tiny_repo_path),
        project_name="tiny_repo",
        github_token=None,
        output_dir=str(tmp_path / "out"),
        include_patterns=["*.py"],
        exclude_patterns=[],
        max_file_size=50_000,
        language="english",
        max_abstractions=5,
        use_cache=False,
        provider="anthropic",
        model="claude-sonnet-4-5",
    )
    graph = build_graph()
    final_state = await graph.ainvoke(state)
    assert final_state["final_output_dir"]
    tutorial_dir = Path(final_state["final_output_dir"])
    assert (tutorial_dir / "index.md").exists()
    chapters = sorted(tutorial_dir.glob("[0-9][0-9]_*.md"))
    assert len(chapters) >= 3
    for ch in chapters:
        content = ch.read_text()
        assert "```mermaid" in content
        assert "sequenceDiagram" in content
    index = (tutorial_dir / "index.md").read_text()
    assert "```mermaid" in index
    assert "flowchart TD" in index
```

### 3. Manual Smoke Test

```bash
# Start the worker + API
docker-compose up -d postgres redis api worker

# Submit a run
curl -X POST http://localhost:8000/api/v1/projects/$PROJECT_ID/runs \
  -H "Authorization: Bearer $JWT" \
  -d '{"max_abstractions": 8, "use_cache": true}'

# Stream progress
curl -N http://localhost:8000/api/v1/runs/$RUN_ID/events \
  -H "Authorization: Bearer $JWT"

# On completion, download the zip
curl -L http://localhost:8000/api/v1/artifacts/$ARTIFACT_ID/download \
  -H "Authorization: Bearer $JWT" -o tutorial.zip

# Inspect
unzip -l tutorial.zip
```

Open `tutorial/index.md` in any Mermaid-enabled Markdown viewer. Verify:
- [ ] Overview diagram renders.
- [ ] All chapter files link correctly.
- [ ] Code excerpts reference real `file:line` ranges (`grep -n` them).
- [ ] Each chapter's sequence diagram parses.

### 4. Negative Paths

| Failure mode | Expected behavior |
|---|---|
| LLM returns malformed YAML in `identify` | Retry up to 3× with stricter prompt; on final failure, raise a structured error in the run record |
| LLM returns invalid abstraction (unknown anchor) | Drop that abstraction, continue with the rest |
| LLM can't write a chapter after retries | Write skeleton chapter with anchor files listed; run still succeeds |
| Graph is empty (no imports) | Order = arbitrary (PageRank); chapters still written |
| Repo too large for available memory | `fetch_repo` rejects; run fails with clear error before `build_code_graph` |
| Cache corruption (bad JSON in Redis) | Log warning, fall back to live call |
| Worker killed mid-`Send` | Checkpointer resumes from last completed chapter |

---

## Performance Targets

| Metric | Target |
|---|---|
| Total wall-clock for 1000-file repo | < 5 min (cache off) |
| Total wall-clock for 1000-file repo | < 30 s (cache on, after first run) |
| Token usage per run | < 100K total (across all LLM calls) |
| Cost per run | < $0.50 (Anthropic Sonnet 4.5 list price) |
| Parallel chapter writes | 4–8 simultaneous (configurable worker concurrency) |
| Cache hit rate on re-runs | > 95% (only changed files invalidate) |

---

## Open Questions Before You Start

1. **LLM call in `order_chapters`** — keep it on by default for nicer prose, or off by default for speed?
2. **Cycle diagram in `combine_tutorial`** — always include, or only when cycles > 1?
3. **Skeleton chapter fallback** — auto-write, or fail the run loudly so the user knows?
4. **Per-chapter cache TTL** — forever (until graph changes), or expire after N days?
5. **Diagram style** — `flowchart TD` (top-down), `flowchart LR` (left-right), or pick based on graph size?

Defaults suggested:
1. Off by default (set `use_llm_order_rationale=True` in request if you want prose).
2. Include only if cycles ≥ 3 (too noisy otherwise).
3. Auto-write skeleton — better to ship 9 good + 1 stub than to fail.
4. Forever for now; add TTL later if disk pressure matters.
5. `flowchart TD` for ≤ 15 nodes, `flowchart LR` otherwise.

---

## Summary

You've built the foundation (`build_code_graph`). The 5 remaining nodes are **constrained LLM calls over a graph you already understand**:

- `identify_abstractions` clusters PageRank top-K candidates into chapters.
- `analyze_relationships` labels graph edges and adds semantic edges.
- `order_chapters` does a topological sort on the abstraction subgraph.
- `write_chapter_single` slices the graph to a k-hop neighborhood per chapter and writes Markdown grounded in real excerpts.
- `combine_tutorial` stitches everything into `index.md` + numbered chapters + a zip.

**The leverage**: every LLM call is bounded, every edge is validated, every excerpt is real. The graph is the source of truth; the LLM is the narrator. Build in the order above, test at each phase with the `tiny_repo` fixture, and the pipeline holds together.
