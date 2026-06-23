from src.codebase_kb.extract.ast_python import parse_python_file
from src.codebase_kb.extract.graph import CodeGraph
import networkx as nx

def build_code_graph_node(state: dict) -> dict:
    """Node 2: Parses the raw text into the NetworkX AST Graph."""
    cg = CodeGraph()
    
    for f in state["files"]:
        if f["path"].endswith(".py"):
            nodes, edges = parse_python_file(f["path"], f["content"])

            cg.add_nodes(nodes)
            cg.add_edges(edges)
        
    serialized_graph = nx.node_link_data(cg.g)
    return {"code_graph": serialized_graph}