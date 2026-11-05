
import asyncio
import json
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


async def main():
    server = StdioServerParameters(
        command="docker",
        args=[
            "run",
            "--rm",
            "-i",
            "-e", "GITHUB_TOOLSETS=repos,users",
            "ghcr.io/github/github-mcp-server:latest",
            "stdio",
            "--read-only",
        ],
    )

    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()

                print("\nConnected to:", result.serverInfo.name)
                print("Version:", result.serverInfo.version)

                tools = await session.list_tools()

                print("\nAVAILABLE MCP TOOLS")
                for tool in tools.tools:
                    print(f"\nTool: {tool.name}")
                    print("Description:", tool.description)
                    print("Input schema:")
                    print(json.dumps(tool.inputSchema, indent=2))

    except Exception as e:
        print("\nERROR:", repr(e))


if __name__ == "__main__":
    asyncio.run(main())
