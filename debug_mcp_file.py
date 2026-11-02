import asyncio
import os
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from config.settings import settings


async def main():
    token = os.getenv("GITHUB_MCP_TOKEN")

    if not token:
        raise RuntimeError("GITHUB_MCP_TOKEN is not set.")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    async with streamablehttp_client(
        settings.github_mcp_url,
        headers=headers
    ) as (read, write, _):

        async with ClientSession(read, write) as session:
            await session.initialize()

            print("MCP INITIALIZED")

            # Test 1: List repository root.
            print("\nTEST 1: Repository root")

            result = await session.call_tool(
                "get_file_contents",
                {
                    "owner": "monishasrinivas-04",
                    "repo": "Urban-Heat-Mapping-using-YOLOv8",
                    "path": "/",
                }
            )

            print("isError:", result.isError)
            print("structuredContent:", getattr(
                result, "structuredContent", None
            ))

            for item in result.content:
                if hasattr(item, "text"):
                    print("TEXT:", item.text[:3000])

            # Test 2: Read README directly.
            print("\nTEST 2: README.md")

            result = await session.call_tool(
                "get_file_contents",
                {
                    "owner": "monishasrinivas-04",
                    "repo": "Urban-Heat-Mapping-using-YOLOv8",
                    "path": "README.md",
                }
            )

            print("isError:", result.isError)

            for index, item in enumerate(result.content):
                print(f"\nCONTENT BLOCK {index}")
                print("TYPE:", type(item).__name__)
                print("OBJECT:", repr(item)[:5000])

                if hasattr(item, "text"):
                    print("TEXT:", item.text[:3000])

                if hasattr(item, "resource"):
                    print("RESOURCE:", repr(item.resource)[:5000])

            print("isError:", result.isError)
            print("structuredContent:", getattr(
                result, "structuredContent", None
            ))

            for item in result.content:
                if hasattr(item, "text"):
                    print("TEXT:", item.text[:3000])


if __name__ == "__main__":
    asyncio.run(main())