
import asyncio
import json
import os
from getpass import getpass

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


async def main():
    token = getpass("Enter GitHub PAT (input hidden): ").strip()

    if not token:
        print("No token entered. Test cancelled.")
        return

    server = StdioServerParameters(
        command="docker",
        args=[
            "run", "--rm", "-i",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "-e", "GITHUB_TOOLSETS=context,repos,users",
            "ghcr.io/github/github-mcp-server:latest",
            "stdio", "--read-only",
        ],
        env={
            **os.environ,
            "GITHUB_PERSONAL_ACCESS_TOKEN": token,
        },
    )

    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}

                print("\nMCP connection successful.")
                print("Account identity tool available:", "get_me" in names)
                print(
                    "Repository discovery tool available:",
                    "search_repositories" in names,
                )

                if "get_me" in names:
                    result = await session.call_tool("get_me", {})
                    print("Account identity call successful:", not result.isError)

                if "search_repositories" in names:
                    result = await session.call_tool(
                        "search_repositories",
                        {
                            "query": "is:private",
                            "minimal_output": True,
                            "perPage": 10,
                        },
                    )
                    print("Private repository search successful:", not result.isError)

                    # Print only the result structure, never repository names.
                    structured = getattr(result, "structuredContent", None)
                    if isinstance(structured, dict):
                        print("Result fields:", list(structured.keys()))

    except Exception as exc:
        print("\nTest failed:", type(exc).__name__)

    finally:
        token = None


if __name__ == "__main__":
    asyncio.run(main())
