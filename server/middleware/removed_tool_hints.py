"""Answer calls to the 7 tools removed in #492 with the current path (#1030).

External clients still call these names: the live ChatGPT directory listing
(v1.0.0) was built against the pre-#492 tool surface, and any client with a
cached tools/list does the same. FastMCP's answer is a bare unknown-tool
error, which tells the model nothing about what replaced the tool, so the
conversation dies or the model invents a workaround. The real fix for the
stale listing is refreshing the OpenAI portal entry (#721, a human task);
this middleware is the server-side mitigation, and it stays correct even
after that refresh because cached tool lists outlive listings.

A FastMCP TOOL middleware, added with ``mcp.add_middleware()`` -- it needs
the tool name, which exists only inside the JSON-RPC body. It is registered
ABOVE ToolScopeEnforcementMiddleware (first-added is outermost) so a removed
name gets its hint even when the caller's scopes would also have denied it:
a scope denial for a tool that does not exist would send the caller off to
re-authorize for nothing.

A name that is neither registered nor in this dict passes through untouched
and surfaces as FastMCP's normal unknown-tool error. Only the names we KNOW
were removed get a hint; guessing at unknown names would turn every typo
into confident misdirection.
"""

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

# The #492 removal set (plus convert_document_to_html, added by PR #382 and
# removed with the rest). Keys are the exact names external clients still
# send. Values name only CURRENTLY REGISTERED tools -- a test cross-checks
# every snake_case token in these strings against the live tool surface, so
# a hint can never point at a tool that does not exist. Do not mention the
# removed name itself inside its hint: the caller knows what it called, and
# the cross-check would rightly reject it.
REMOVED_TOOL_HINTS: dict[str, str] = {
    "generate_template_from_prompt": (
        "This tool was removed. You are the intelligence now: design the "
        "template yourself from the request, then build it with "
        "create_template, add_step_to_template and add_kickoff_field."
    ),
    "generate_template_from_document": (
        "This tool was removed. You are the intelligence now: read the "
        "document yourself, extract the steps, then build the template with "
        "create_template and add_step_to_template."
    ),
    "generate_template_from_image": (
        "This tool was removed. You are the intelligence now: interpret the "
        "image yourself, then build the template with create_template and "
        "add_step_to_template."
    ),
    "update_fields_from_file": (
        "This tool was removed. You are the intelligence now: extract the "
        "values from the file yourself, then write them with "
        "update_form_field or update_kickoff_field."
    ),
    "convert_document_to_html": (
        "This tool was removed. You are the intelligence now: write the HTML "
        "yourself and put it where it is needed, for example a step "
        "description via edit_description_on_step."
    ),
    "suggest_instance_name": (
        "This tool was removed. You are the intelligence now: choose the "
        "name yourself following the naming guidance in launch_process, "
        "then launch with launch_process."
    ),
    "suggest_procedure_steps": (
        "This tool was removed. You are the intelligence now: design the "
        "steps yourself, then add them with add_step_to_template."
    ),
}


class RemovedToolHintsMiddleware(Middleware):
    """Convert a known-removed tool name into an actionable ToolError."""

    async def on_call_tool(self, context, call_next):
        # Read the name the same defensive way tool_scope_enforcement does:
        # a shape we cannot read is not ours to answer -- pass it through so
        # the scope middleware's own no-name refusal stays the single owner
        # of that case.
        message = getattr(context, "message", None)
        name = getattr(message, "name", None)
        if isinstance(name, str) and name in REMOVED_TOOL_HINTS:
            raise ToolError(REMOVED_TOOL_HINTS[name])
        return await call_next(context)
