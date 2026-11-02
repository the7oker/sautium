"""A one-tool stdio MCP server for the agent-tool tests: `ping` appends its
note to the file PROBE_LOG names and answers "pong", so a test sees which
calls an agent CLI actually let through."""

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("probe")


@server.tool()
def ping(note: str = "") -> str:
    """Answer pong."""
    with open(os.environ["PROBE_LOG"], "a", encoding="utf-8") as f:
        f.write(note + "\n")
    return "pong"


if __name__ == "__main__":
    server.run()
