
import asyncio
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


async def main():
    server = StdioServerParameters(
        command="docker",
        args=[
            "run", "--rm", "-i",
            "-e", "GITHUB_TOOLSETS=repos,users",
            "ghcr.io/github/github-mcp-server:latest",
            "stdio", "--read-only",
        ],
    )

    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                print("Connected to GitHub MCP Server.")

                result = await session.call_tool(
                    "get_file_contents",
                    {
                        "owner": "octocat",
                        "repo": "Hello-World",
                        "path": "README",
                    },
                )

                print("\nPUBLIC REPOSITORY RESULT:")
                for item in result.content:
                    if hasattr(item, "text"):
                        print(item.text)

                if result.isError:
                    print("\nThe MCP tool reported an error.")

    except Exception as exc:
        print("\nTEST FAILED:")
        print(type(exc).__name__, str(exc))


if __name__ == "__main__":
    asyncio.run(main())
