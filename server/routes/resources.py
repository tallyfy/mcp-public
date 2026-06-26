"""
MCP Resources

Provides MCP resource endpoints for tool discovery and metadata.
"""


def register_resources(mcp):
    """Register MCP resources with the server."""

    @mcp.resource("tallyfy://tools")
    async def get_available_tools() -> str:
        """Get a list of all available Tallyfy tools"""
        tools = []
        for tool in await mcp.list_tools():
            name = tool.name
            desc = (tool.description or "").split("\n")[0].strip()
            tools.append(f"• {name} - {desc}" if desc else f"• {name}")
        tools.sort()
        return "\n".join(tools)
