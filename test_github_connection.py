
import asyncio
from getpass import getpass

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from config.settings import settings


async def main():
    print("GitHub MCP connection test")
    print("-" * 40)

    token = getpass(
        "Enter GitHub token (press Enter to test without one): "
    ).strip()

    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with streamablehttp_client(
            settings.github_mcp_url,
            headers=headers
        ) as (read, write, _):

            async with ClientSession(read, write) as session:
                await session.initialize()

                print("\nMCP INITIALIZATION: SUCCESS")

                response = await session.list_tools()
                tools = response.tools

                print("TOOLS DISCOVERED:", len(tools))

                for tool in tools:
                    print(f"\nTool: {tool.name}")
                    print("Description:", tool.description or "Not provided")
                    print("Input schema:", tool.inputSchema)

                expected = {
                    "search_code",
                    "get_file_contents",
                    "get_file_content",
                }

                available = {tool.name for tool in tools}

                print("\nExpected tool availability:")
                for name in expected:
                    print(f"{name}: {name in available}")

    except Exception as exc:
        import traceback

        print("\nMCP CONNECTION FAILED")
        print("Error type:", type(exc).__name__)
        traceback.print_exception(exc)
if __name__ == "__main__":
    asyncio.run(main())
