import sys
from pathlib import Path
import os
import asyncio
from unittest.mock import AsyncMock, patch

# Add src to pythonpath so imports work
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

# Set a dummy secret key so the router cipher doesn't crash upon import
os.environ["APP_SECRET_KEY"] = "dummy_secret_for_testing"

from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
from src.codebase_kb.graph.graph import build_graph

async def main():
    hf_token = os.getenv("HUGGINGFACE_API_KEY", "dummy_token")
    os.environ["HUGGINGFACE_API_KEY"] = hf_token

    print("Setting up HuggingFace LLM endpoint...")
    try:
        # Use a free and accessible HuggingFace chat model
        llm_endpoint = HuggingFaceEndpoint(
            repo_id="deepseek-ai/DeepSeek-V4-Pro",
            huggingfacehub_api_token=hf_token,
            temperature=0.2,
            task="text-generation",
            max_new_tokens=4096
        )
        mock_llm = ChatHuggingFace(llm=llm_endpoint)
    except Exception as e:
        print(f"Failed to initialize HuggingFace Endpoint: {e}")
        sys.exit(1)

    # We patch the router so it bypasses the database API key lookup and uses our HF model directly
    with patch("codebase_kb.graph.nodes.identify_abstractions_node.get_provider_for_user", new_callable=AsyncMock) as mock_get_provider1, \
         patch("codebase_kb.graph.nodes.analyze_relationships.get_provider_for_user", new_callable=AsyncMock) as mock_get_provider2:
        mock_get_provider1.return_value = mock_llm
        mock_get_provider2.return_value = mock_llm
        
        print("Compiling LangGraph...")
        graph = build_graph()
        
        # Test input state using a very small public repo that actually contains Python code
        repo_url = "https://github.com/KaiAllAlone/Smart_Day_Planner_Agno_Framework"
        
        state = {
            "run_id": "test_run_123",
            "user_id": "test_user",
            "client_id":os.getenv("client_id","") ,
            "repo_url": repo_url,
            "github_token": os.getenv("GITHUB_TOKEN", ""), # Optional
            "provider": "huggingface",
            "max_abstractions": 2,
            "language": "english"
        }
        
        config = {
            "configurable": {
                "db_session": AsyncMock() # Mock the database session entirely
            }
        }

        print(f"Running graph for repo: {repo_url}...")
        try:
            # Note: The identify_abstractions_node will print to logger when parsing. 
            # If HF inference API returns empty, it will throw an error.
            result = await graph.ainvoke(state, config=config)
            print("\n=== Workflow Completed Successfully! ===")
            print("\nIdentified Abstractions:")
            print(result)
            # for abs_ in result.get("abstractions", []):
            #     print(abs_)
            #     print(f" - {abs_.get('name')}: {abs_.get('description')}")
            
            print("\nArchitectural Summary:")
            print(result.get("summary", "No summary available."))
            
            print("\nIdentified Relationships:")
            for rel in result.get("relationships", []):
                print(f" - [{rel.get('from')}] --({rel.get('label')} [{rel.get('kind')}])--> [{rel.get('to')}]")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"\nWorkflow failed with error: {e}")
        
        # print("\n=== FINAL STATE KEYS ===")
        # for key in result.keys():
        #     print(f"- {key}")
            
        # print("\n=== SOME STATE DETAILS ===")
        #     # Print how many files were fetched
        # if "files" in result:
        #     print(f"Total files fetched: {len(result['files'])}")
            
        #     # Print how many nodes/edges are in the code graph
        # if "code_graph" in result:
        #     nodes = result["code_graph"].get("nodes", [])
        #     edges = result["code_graph"].get("edges", [])
        #     print(f"Code Graph size: {len(nodes)} nodes, {len(edges)} edges")
            
        #     # If you REALLY want to print everything (warning: massive output):
        #     # import pprint
        #     # pprint.pprint(result)


if __name__ == "__main__":
    asyncio.run(main())
