
from llm.cloudflare_client import generate_answer

print("Testing Cloudflare Qwen inference...")

try:
    answer, latency = generate_answer(
        question="What is FreshTrust-MCP?",
        context=(
            "FreshTrust-MCP is a repository intelligence system "
            "using GitHub MCP, semantic RAG, LangGraph and Qwen."
        ),
        max_tokens=1024
    )

    print("\nSUCCESS")
    print("Model response:")
    print(answer)
    print(f"\nLatency: {latency:.2f} seconds")

except Exception as exc:
    print("\nTEST FAILED")
    print("Error:", type(exc).__name__)
    print("Details:", str(exc))
