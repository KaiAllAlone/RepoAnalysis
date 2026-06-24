from codebase_kb.extract.graph import CodeGraph
from langchain_core.runnables import RunnableConfig
from codebase_kb.observability.logging import get_logger
from codebase_kb.llm.router import get_provider_for_user
from codebase_kb.prompts.render import render_prompt
from langchain_core.messages import HumanMessage
from codebase_kb.utils.json_parse import extract_json_object

log = get_logger(__name__)

# the output will look like this 
# [{"from_name": "Authentication", "to_name": "Database Models", "kind": "import", "src_count": 15}]

def _build_abstraction_edges(code_graph_payload: dict, abstractions: list[dict]) -> list[dict]:
    g = CodeGraph.from_payload(code_graph_payload)
    # map node to abstraction so that from the node we can easily find out which abstraction it belongs to
    node_to_abs: dict[str, str] = {}
    for abs_ in abstractions:
        for nid in abs_.get("anchor_node_ids", []):
            node_to_abs[nid] = abs_.get("name")

    # frequency counter: (src, dest, kind) -> count
    bag: dict[tuple[str, str, str], int] = {}
    for u, v, data in g.g.edges(data = True):
        from_abs = node_to_abs.get(u)
        to_abs = node_to_abs.get(v)
        # if either node doesn't belong to any abstraction or they both belong to the same abstraction we skip as we are finding inter-abstraction edges
        if not from_abs or not to_abs or from_abs == to_abs:
            continue
        key = (from_abs, to_abs, data.get("kind", "unknown"))
        bag[key] = bag.get(key, 0) + 1
    # return as json
    return [
        {"from_name": k[0], "to_name": k[1], "kind": k[2], "src_count": c}
        for k, c in bag.items()
    ]

async def analyze_relationships_node(state: dict, config: RunnableConfig) -> dict:
    log.info("analyze_relationships.start, run_id=%s", state.get("run_id"))
    db_session = config.get("configurable", {}).get("db_session")
    provider = await get_provider_for_user(state["user_id"], state["provider"], db_session)

    edges = _build_abstraction_edges(state["code_graph"], state["abstractions"])
    prompt = render_prompt(
        "analyze",
        abstractions=state["abstractions"],
        edges=edges,
        language=state.get("language", "english"),
    )
    response = await provider.ainvoke([HumanMessage(content=prompt)])
    raw = response.content if isinstance(response.content, str) else str(response.content)
    parsed = extract_json_object(raw)
    if parsed is None:
        raise ValueError("analyze_relationships: LLM did not return a JSON object")

    abs_names = {a["name"] for a in state["abstractions"]}
    relationships = []
    for r in parsed.get("relationships", []):
        if r.get("from") not in abs_names or r.get("to") not in abs_names:
            continue
        if r["from"] == r["to"]:
            continue
        relationships.append({
            "from": r["from"],
            "to": r["to"],
            "label": (r.get("label") or "uses")[:60],
            "kind": r.get("kind", "semantic"),
        })
    
    # Self-edge placeholder for uncovered abstractions
    covered = {r["from"] for r in relationships} | {r["to"] for r in relationships}
    for m in abs_names - covered:
        relationships.append({"from": m, "to": m, "label": "self-contained", "kind": "semantic"})

    summary = parsed.get("summary", "")
    log.info("analyze_relationships.done, edges=%s", len(relationships))
    return {"relationships": relationships, "summary": summary}