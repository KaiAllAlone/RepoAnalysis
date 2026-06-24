import operator
from typing import Annotated, Any, TypedDict


class Chapter(TypedDict, total=False):
    index: int
    name: str
    markdown: str
    path: str


class KnowledgeBuilderState(TypedDict, total=False):
    # Inputs
    run_id: str
    project_id: str
    org_id: str
    user_id: str
    client_id: str
    repo_url: str
    github_token: str
    output_dir: str
    project_name: str
    include_patterns: list[str]
    exclude_patterns: list[str]
    max_file_size: int
    language: str
    max_abstractions: int
    use_cache: bool
    provider: str
    model: str | None
    use_llm_order_rationale: bool

    # Produced by fetch_repo
    files: list[dict[str, str]]                # [{"path": ..., "content": ...}]

    # Produced by build_code_graph
    code_graph: dict[str, Any]                 # {nodes, edges, metrics, _meta.graph_hash}

    # Produced by identify_abstractions
    abstractions: list[dict[str, Any]]

    # Produced by analyze_relationships
    relationships: list[dict[str, Any]]
    summary: str

    # Produced by order_chapters
    chapter_order: list[int]
    rationale: str

    # Produced by write_chapter_single (merged via reducer)
    chapters: Annotated[list[Chapter], operator.add]

    # Produced by combine_tutorial
    final_output_dir: str
    artifacts: list[dict[str, Any]]

    # Cross-cutting
    errors: list[dict[str, Any]]
