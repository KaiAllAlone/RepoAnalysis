from langgraph.graph import StateGraph, START, END
from codebase_kb.graph.state import KnowledgeBuilderState
from codebase_kb.graph.nodes.fetch_repo import fetch_repo_node
from codebase_kb.graph.nodes.build_code_graph_node import build_code_graph_node
from codebase_kb.graph.nodes.identify_abstractions_node import identify_abstractions_node
from codebase_kb.graph.nodes.analyze_relationships import analyze_relationships_node

def build_graph():
    g = StateGraph(KnowledgeBuilderState)
    g.add_node("fetch_repo", fetch_repo_node)
    g.add_node("build_code_graph", build_code_graph_node)
    g.add_node("identify_abstractions", identify_abstractions_node)
    g.add_node("analyze_relationships", analyze_relationships_node)

    g.add_edge(START, "fetch_repo")
    g.add_edge("fetch_repo", "build_code_graph")
    g.add_edge("build_code_graph", "identify_abstractions")
    g.add_edge("identify_abstractions", "analyze_relationships")
    g.add_edge("analyze_relationships", END)

    return g.compile()