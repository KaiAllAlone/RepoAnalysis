import sys
from pathlib import Path
import matplotlib.pyplot as plt

sys.path.append(str(Path(__file__).resolve().parents[1]))

import os
import networkx as nx
from src.codebase_kb.extract.graph import CodeGraph
from src.codebase_kb.extract.ast_python import parse_python_file

# (Assume parse_python_file and CodeGraph are imported here)

def test_on_real_repo(repo_path: str):
    cg = CodeGraph()
    
    print(f"Scanning repository: {repo_path}")
    # 1. Walk the directory and parse all Python files
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.endswith(".py"):
                full_path = os.path.join(root, file)
                
                # Keep paths relative to repo root for cleaner output
                rel_path = os.path.relpath(full_path, repo_path)
                
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    
                # Parse the file using our AST logic
                nodes, edges = parse_python_file(rel_path, content)
                cg.add_nodes(nodes)
                cg.add_edges(edges)
                
    print(f"Graph built successfully!")
    print(f"Total Nodes: {cg.g.number_of_nodes()}")
    print(f"Total Edges: {cg.g.number_of_edges()}")
    nx.draw(cg.g, with_labels=True)
    plt.show()
    
    if cg.g.number_of_nodes() == 0:
        print("Empty graph. Did you point it to a valid Python repo?")
        return

    # 2. Test PageRank (Are the Core Abstractions accurate?)
    print("\n--- Top 10 Core Concepts (PageRank) ---")
    top_nodes = cg.core_abstractions(k=10)
    for node_id, score in top_nodes:
        # Get the node data to print the human-readable name
        node_data = cg.g.nodes[node_id]
        name = node_data.get('name', node_id)
        kind = node_data.get('kind', 'unknown')
        print(f"Score: {score:.4f} | [{kind.upper()}] {name}")

    # 3. Test Communities (Are the chapter groupings logical?)
    print("\n--- Architectural Communities (Chapters) ---")
    communities = cg.communities()
    for i, comm in enumerate(communities):
        # Only show communities that have actual internal nodes
        if len(comm) > 0:
            sample = list(comm)[:3] # Show up to 3 examples
            print(f"Chapter {i+1}: Contains {len(comm)} nodes. Sample elements: {sample}")


if __name__ == "__main__":
    # Change this to the path where you cloned a repo like 'requests'
    test_on_real_repo("C:/Users/HP/OneDrive/Desktop/RepoAnalysis/Q-Learning-From-Scratch")
