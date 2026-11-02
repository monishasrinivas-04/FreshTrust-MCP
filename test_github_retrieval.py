"""Smoke test for MCP-only GitHub repository retrieval."""

import asyncio
import getpass
import traceback

from mcp_layer.github_client import GitHubMCPClient


async def main():
    owner = input("GitHub owner [monishasrinivas-04]: ").strip()
    if not owner:
        owner = "monishasrinivas-04"

    repo = input(
        "Repository [Urban-Heat-Mapping-using-YOLOv8]: "
    ).strip()
    if not repo:
        repo = "Urban-Heat-Mapping-using-YOLOv8"

    visibility = input("Visibility (Public/Private) [Public]: ").strip()
    if not visibility:
        visibility = "Public"

    token = ""
    if visibility.lower() == "private":
        token = getpass.getpass("GitHub PAT (hidden): ").strip()
        if not token:
            print("Private repository access requires a GitHub PAT.")
            return

    question = input(
        "What should MCP search for? [project architecture and implementation]: "
    ).strip()
    if not question:
        question = "project architecture and implementation"

    print("\nStarting GitHub MCP retrieval...")
    print(f"Repository: {owner}/{repo}")
    print(f"Visibility: {visibility}")
    print("Retrieval transport: GitHub MCP only")

    try:
        client = GitHubMCPClient(token=token or None)
        files = await client.retrieve(
            owner=owner,
            repo=repo,
            question=question,
            max_files=6,
            visibility=visibility,
        )

        print(f"\nSUCCESS: MCP retrieved {len(files)} file(s).")
        for item in files:
            print("\n" + "=" * 72)
            print("FILE:", item["path"])
            print("CHARACTERS:", len(item["content"]))
            print("PREVIEW:")
            print(item["content"][:700])

    except Exception as exc:
        print("\nMCP RETRIEVAL FAILED")
        print(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
