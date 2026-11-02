import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from config.settings import settings


async def main():
    token = os.getenv("GITHUB_MCP_TOKEN")

    print("Token available:", bool(token))
    print("MCP endpoint:", settings.github_mcp_url)

    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with streamablehttp_client(
        settings.github_mcp_url,
        headers=headers
    ) as (read_stream, write_stream, _):

        async with ClientSession(read_stream, write_stream) as session:

            await session.initialize()

            print("\nMCP INITIALIZATION: SUCCESS")

            response = await session.list_tools()

            for tool in response.tools:
                if tool.name in [
                    "get_file_contents",
                    "search_code",
                    "list_commits"
                ]:
                    print("\nTOOL:", tool.name)
                    print("DESCRIPTION:", tool.description)
                    print("INPUT SCHEMA:", tool.inputSchema)


if __name__ == "__main__":
    asyncio.run(main())