"""
MCP Resources

Provides MCP resource endpoints for tool discovery and metadata.
"""

from utils.variable_markup import VARIABLE_MARKUP_URI, variable_markup_reference


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

    @mcp.resource(VARIABLE_MARKUP_URI)
    async def get_variable_markup() -> str:
        """The exact markup for embedding a form field, snippet, blueprint,
        document field or mention in Tallyfy rich text (#1292).

        Served as a resource rather than written into each tool description
        because the descriptions are hard-capped at 2000 BYTES and the tightest
        of the seven that write HTML has 71 to spare. Each of them carries a
        one-line pointer here instead.
        """
        return variable_markup_reference()
