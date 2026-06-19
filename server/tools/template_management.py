"""
Template Management Tools
Tools for managing templates, steps, and template health
"""

import re
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    TemplateId,
    TemplateTitle,
    StepId,
    FieldId,
    StepDescription,
    StepPosition,
    OptionalString,
    OptionalBool,
    PageNumber,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from utils.pagination import fetch_single_page
from metrics import track_tool_execution


def _is_tallyfy_id(value: str) -> bool:
    return bool(re.fullmatch(r'[0-9a-f]{32}', value.lower()))


def _resolve_template_folder_name_to_id(sdk, org_id: str, folder_name: str) -> str:
    """Resolve a folder name to its ID by searching template folders (including nested children)."""
    try:
        folders = sdk.folders.get_folders(org_id, folder_type='checklist')
        if folders:
            match = _find_folder_by_name(folders, folder_name)
            if match:
                return str(match.id)
        raise ToolError(
            f"Template folder '{folder_name}' not found. Use get_template_folders to see available folders."
        )
    except ToolError:
        raise
    except Exception:
        return folder_name


def _find_folder_by_name(folders, name: str):
    """Recursively search folders and children for a name match."""
    for f in folders:
        if hasattr(f, "name") and f.name and f.name.lower() == name.lower():
            return f
        if hasattr(f, "children") and f.children:
            match = _find_folder_by_name(f.children, name)
            if match:
                return match
    return None


def register_template_management_tools(mcp):
    """Register all template management tools with the MCP server"""

    @mcp.tool(
        name="get_template",
        description="""Get a template (checklist) by its ID or name with full details.

MANDATORY: You MUST provide either 'template_id' OR 'template_name'. Calling with empty parameters WILL FAIL.

CORRECT usage examples:
- get_template(template_id="abc123def456") - when you have the template ID from a previous result
- get_template(template_name="Employee Onboarding") - when you know the template name

WRONG usage (will fail):
- get_template() - NO! Missing required parameter
- get_template(template_id="", template_name="") - NO! Must provide a value for one

If you don't have a template_id or name, use search_for_templates(query="...") first to find templates, or use get_all_templates() to list all templates.

DO NOT use this tool just to list steps — use get_template_steps instead.""",
        tags=["templates", "blueprints", "read-only"],
        annotations=ToolAnnotations(
            title="Get template",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_template")
    @handle_tallyfy_errors("get template")
    def get_template(
        template_id: OptionalString = "",
        template_name: OptionalString = "",
    ) -> GenericDict:
        """
        Get a template (checklist) by its ID or name with full details.

        Args:
            template_id: Template (checklist) ID (provide this OR template_name, not both)
            template_name: Template (checklist) name (provide this OR template_id, not both)

        Returns:
            Template object with complete template data
        """
        if not template_id.strip() and not template_name.strip():
            raise ToolError("Either template_id or template_name must be provided")

        if template_id.strip() and template_name.strip():
            raise ToolError("Only one of template_id or template_name should be provided")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            if template_name.strip():
                template = sdk.templates.get_template(org_id, template_name=template_name.strip())
            else:
                template = sdk.templates.get_template(org_id, template_id=template_id.strip())
            return ToolResult(
                content=serialize_dataclass(template) if template else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_all_templates",
        description="Get templates (checklists) with full details including prerun fields, automated actions, linked tasks, and metadata. Returns 20 per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count. Optional: filter by folder name or folder ID.",
        tags=["templates", "blueprints", "read-only", "management"],
        annotations=ToolAnnotations(
            title="Get all templates",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_all_templates")
    @handle_tallyfy_errors("get all templates")
    def get_all_templates(page: PageNumber = 1, folder: OptionalString = None) -> GenericDict:
        """
        Get templates (checklists) with full details.

        Args:
            page: Page number to fetch (default: 1)
            folder: Optional folder ID (32-char hex) or folder name to filter templates by folder

        Returns:
            Dict with 'data' (list of templates) and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            kwargs = {}
            if folder:
                if not _is_tallyfy_id(folder):
                    folder = _resolve_template_folder_name_to_id(sdk, org_id, folder)
                kwargs["folder"] = folder
            return ToolResult(
                content=fetch_single_page(
                    sdk.templates.get_all_templates, org_id,
                    page=page,
                    compact_fields=["guidance"],
                    **kwargs,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="get_step_dependencies",
        description="""Read-only: analyze which automations affect when this step appears in the workflow. Inspects all template rules referencing the step as condition trigger OR action target.

RETURN: {step_info: {id,title,position,summary}, dependencies: {incoming: [{step_id,step_title,condition_type,automation_id,description}], outgoing: [{step_id,step_title,action_type,automation_id,description}], field_dependencies: [{field_label,expected_value,condition_type,automation_id,description}], conditional_visibility: [{action_type:"show_step"|"hide_step",automation_id,description}]}, complexity_analysis: {score:0-100, level:"Low"|"Medium"|"High", total_dependencies, incoming_count, outgoing_count, field_dependencies_count, visibility_conditions_count}, recommendations: [advisory strings], template_id}

KEY: `conditional_visibility` lists automation_ids of show/hide rules for this step. Look them up via `analyze_template_automations` or `get_step_visibility_conditions` for full conditions.

USE CASES: "What does this step depend on?"→incoming · "What does this step trigger?"→outgoing · "Which fields gate it?"→field_dependencies · "Is visibility conditional?"→conditional_visibility · "Should this step be split?"→complexity_analysis.level

EXAMPLE: get_step_dependencies(template_id="58c03f...", step_id="9bc2...") → {step_info:{title:"Manager approval",position:4}, dependencies:{incoming:[{step_title:"Submit request",condition_type:"task_completed"}], outgoing:[{step_title:"Notify employee",action_type:"send_email"}], field_dependencies:[{field_label:"Amount",expected_value:">1000"}]}, complexity_analysis:{score:35,level:"Medium"}, recommendations:["Consider extracting the amount-gate"]}

REQUIRED: 'template_id' AND 'step_id' (both 32-char hex). Never call without both.""",
        tags=["templates", "workflow", "analysis", "automation", "read-only"],
        annotations=ToolAnnotations(
            title="Get step dependencies",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_step_dependencies")
    @handle_tallyfy_errors("analyze step dependencies")
    def get_step_dependencies(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Analyze which automations affect when this step appears.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary containing dependency analysis
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.get_step_dependencies(org_id, template_id, step_id)
            return ToolResult(content=serialize_dataclass(result) if result else {}, structured_content=None)

    @mcp.tool(
        name="suggest_step_deadline",
        description="""Retrieve step details with template context to recommend an appropriate deadline.

Returns the step's title, summary, position in the workflow, current deadline (if any), assignees,
and the total number of steps in the template.

Use this data to suggest a reasonable deadline by considering:
- Step complexity (review/approve steps are quick; document creation takes longer)
- Position in workflow (early steps may need faster turnaround)
- Dependencies and assignee count
- Whether the step has form fields that require data gathering

Suggest deadlines using: value (number), unit ('minutes', 'hours', 'days', 'weeks'), and option ('from' = relative to process launch).

REQUIRED: Both 'template_id' and 'step_id' must be provided (32-character hex strings). Never call this without both parameters.""",
        tags=["templates", "workflow", "analysis", "deadlines", "read-only"],
        annotations=ToolAnnotations(
            title="Suggest step deadline",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_step_deadline")
    @handle_tallyfy_errors("suggest step deadline")
    def suggest_step_deadline(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Retrieve step details with template context for deadline recommendation.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with step data and template context for deadline analysis
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            steps = template.steps or []
            target_step = None
            for step in steps:
                if step.id == step_id:
                    target_step = step
                    break

            if not target_step:
                raise ToolError(f"Step {step_id} not found in template {template_id}")

            return ToolResult(
                content={
                    'step': serialize_dataclass(target_step),
                    'template_title': template.title,
                    'total_steps': len(steps),
                    'template_id': template_id,
                },
                structured_content=None
            )

    @mcp.tool(
        name="add_assignees_to_step",
        description="""Add assignees (users or guests or both) to a specific step in a template.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'assignees'.

'assignees' accepts the following format:
Dict with 'users' and/or 'guests' keys (to add guests by email):
assignees: {"users": [10026], "guests": ["alice@example.com"]}
assignees: {"guests": ["alice@example.com"]}
assignees: {"users": [10026]}

Never call this without all three parameters.""",
        tags=["templates", "workflow", "write", "management", "assignees"],
        annotations=ToolAnnotations(
            title="Add assignees to step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_assignees_to_step")
    @handle_tallyfy_errors("add assignees to step")
    def add_assignees_to_step(
        template_id: TemplateId,
        step_id: StepId,
        assignees: Any,
    ) -> GenericDict:
        """
        Add assignees to a specific step in a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to add assignees to (REQUIRED - 32-character hex string)
            assignees: List of numeric user IDs, e.g. [10026, 64878].
                       Also accepts a dict with 'users' and/or 'guests' keys:
                       {"users": [10026, 64878], "guests": ["alice@example.com"]}.

        Returns:
            Dictionary containing updated step information
        """
        # Normalise to a plain list of user IDs + optional guests list
        # The SDK expects add_assignees_to_step(..., assignees: List[int], guests: Optional[List[str]])
        # LLMs often pass stringified JSON or bare values — coerce gracefully
        if isinstance(assignees, str):
            import json
            try:
                assignees = json.loads(assignees)
            except (json.JSONDecodeError, ValueError):
                raise ToolError('assignees must be a dict like {"users": [10026]} or {"guests": ["alice@example.com"]} or {"users": [10026], "guests": ["alice@example.com"]}')

        if not isinstance(assignees, dict):
            raise ToolError('assignees must be a dict like {"users": [10026]} or {"guests": ["alice@example.com"]} or {"users": [10026], "guests": ["alice@example.com"]}')

        user_ids = assignees.get('users', [])
        guest_emails = assignees.get('guests') or None

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.add_assignees_to_step(org_id, template_id, step_id, user_ids, guests=guest_emails)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="edit_description_on_step",
        description="""Edit the description/summary of a specific step in a template. The description supports HTML — use this to add rich instructions, checklists, or converted document content to a step. When a user wants to convert a document to step instructions, read the document content yourself and write the HTML here.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'description' (new text, HTML allowed). Never call this without all three parameters.""",
        tags=["templates", "workflow", "write", "management", "editing"],
        annotations=ToolAnnotations(
            title="Edit step description",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("edit_description_on_step")
    @handle_tallyfy_errors("edit step description")
    def edit_description_on_step(
        template_id: TemplateId,
        step_id: StepId,
        description: StepDescription
    ) -> GenericDict:
        """
        Edit the description/summary of a specific step in a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to edit description for (REQUIRED - 32-character hex string)
            description: New description/summary text for the step (REQUIRED)

        Returns:
            Dictionary containing updated step information
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.edit_description_on_step(org_id, template_id, step_id, description)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="add_step_to_template",
        description="""Add a new step to a template. Call this repeatedly after create_template to build out the workflow structure — one call per step, in order. When building a template from a user description or document, break the workflow into logical steps and call this for each one.

REQUIRED: 'template_id' (32-char hex) and 'step_data' (dict with 'title' field — other fields optional).

step_data keys:
  - 'title': step name (REQUIRED)
  - 'description': HTML instructions for the step assignee
  - 'position': 1-based order in the workflow
  - 'step_type': one of these 5 values (default 'task'):
      'task'           — standard task, completed by assignee
      'approval'       — approve/reject decision (MUST use this for any approval or review step — enables 'approved'/'rejected' automation conditions)
      'expiring'       — auto-completes after deadline passes
      'email'          — sends an email notification
      'expiring_email' — sends email, auto-completes after deadline

IMPORTANT: If a step involves approval, review, or sign-off, set step_type='approval'. Without this, automation rules that trigger on 'approved' or 'rejected' will not work. If a step is a notification or email alert, use 'email'.

Never call this without both parameters.""",
        tags=["templates", "workflow", "write", "management", "creation"],
        annotations=ToolAnnotations(
            title="Add step to template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_step_to_template")
    @handle_tallyfy_errors("add step to template")
    def add_step_to_template(template_id: TemplateId, step_data: GenericDict) -> GenericDict:
        """
        Add a new step to a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_data: Dictionary containing step data including title, summary, position, etc. (REQUIRED - must include 'title')

        Returns:
            Dictionary containing created step information
        """
        if 'title' not in step_data:
            raise ToolError("step_data must contain 'title' field")
        if not step_data['title']:
            raise ToolError("step_data.title must not be empty")

        # Inject checklist_id — required by the Tallyfy API but not always passed by the caller
        step_data = {**step_data, 'checklist_id': template_id}

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.add_step_to_template(org_id, template_id, step_data)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="suggest_kickoff_fields",
        description="""Retrieve template data with existing kickoff fields and step context to recommend new kickoff fields.

Returns the template title, summary, existing prerun/kickoff fields, and step titles/summaries.

Use this data to suggest kickoff fields that would help initialize the workflow by considering:
- What information the steps will need (client names, project details, dates, budgets)
- What existing kickoff fields already capture (avoid duplicates)
- The template's domain and purpose (inferred from title, summary, and step content)
- Field types: text, textarea, date, dropdown, number, file

REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.""",
        tags=["templates", "workflow", "analysis", "kickoff", "read-only"],
        annotations=ToolAnnotations(
            title="Suggest kickoff fields",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_kickoff_fields")
    @handle_tallyfy_errors("suggest kickoff fields")
    def suggest_kickoff_fields(template_id: TemplateId) -> GenericDict:
        """
        Retrieve template data with existing kickoff fields for field suggestions.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with template metadata, existing prerun fields, and step summaries
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            existing_prerun = [serialize_dataclass(f) for f in template.prerun] if template.prerun else []
            steps_summary = []
            if template.steps:
                for s in template.steps:
                    steps_summary.append({
                        'id': s.id,
                        'title': s.title,
                        'summary': s.summary,
                    })

            return ToolResult(
                content={
                    'template_id': template_id,
                    'template_title': template.title,
                    'template_summary': template.summary,
                    'existing_kickoff_fields': existing_prerun,
                    'steps': steps_summary,
                },
                structured_content=None
            )

    @mcp.tool(
        name="get_kickoff_fields",
        description="Get all kickoff/prerun fields for a template. REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.",
        tags=["templates", "kickoff", "prerun", "forms", "read-only"],
        annotations=ToolAnnotations(
            title="Get kickoff fields",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_kickoff_fields")
    @handle_tallyfy_errors("get kickoff fields")
    def get_kickoff_fields(template_id: TemplateId) -> GenericList:
        """
        Get all kickoff/prerun fields for a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            List of kickoff field objects
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Get the template which includes prerun fields
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if template and template.prerun:
                return ToolResult(
                    content=compact_result([serialize_dataclass(field) for field in template.prerun]),
                    structured_content=None
                )
            return ToolResult(content=[], structured_content=None)

    @mcp.tool(
        name="get_template_steps",
        description="""Get all steps for a template in order. USE THIS instead of get_template when the user asks about steps.

MANDATORY: 'template_id' (32-char hex string) is required.

USE THIS TOOL when the user asks:
- "What are the steps in [template]?"
- "List the steps of [template]"
- "Show me the steps for [template]"
- Any question about a template's steps or structure
- Finding a step's ID before editing or assigning it

WORKFLOW: If you don't have the template_id yet:
1. Call search_for_templates(query="<template name>") to get the template_id
2. Then call get_template_steps(template_id="<id>")

CORRECT usage:
- get_template_steps(template_id="abc123...")

DO NOT call get_template just to read its steps — use this tool instead.""",
        tags=["templates", "steps", "workflow", "read-only"],
        annotations=ToolAnnotations(
            title="Get template steps",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_template_steps")
    @handle_tallyfy_errors("get template steps")
    def get_template_steps(template_id: TemplateId) -> GenericList:
        """
        Get all steps for a template in order.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            List of step objects with id, title, position, and other step properties
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            steps = sdk.templates.get_template_steps(org_id, template_id)
            return ToolResult(
                content=compact_result([serialize_dataclass(s) for s in steps]),
                structured_content=None
            )

    @mcp.tool(
        name="assess_template_health",
        description="""Retrieve complete template data for a comprehensive health assessment.

Returns the full template including metadata, steps, automation rules, and kickoff fields.

Use this data to evaluate template health across these dimensions:
- Metadata quality: Does it have a clear title, summary, and guidance?
- Step clarity: Do steps have descriptive titles and summaries? Are any too vague?
- Form completeness: Do steps that need data collection have appropriate form fields?
- Automation efficiency: Are automation rules well-structured? Any conflicts or redundancies?
- Deadline configuration: Do time-sensitive steps have reasonable deadlines?
- Workflow structure: Is the step count manageable? Is the flow logical?

Provide an overall health rating (excellent/good/fair/poor/critical) with specific recommendations.

RETURNS: full template payload — top-level keys include `id`, `title`, `summary`, `steps[]`, `automated_actions[]`, `prerun[]` (kickoff fields), and metadata. Synthesize this into a `health_rating` (one of: excellent, good, fair, poor, critical) plus a `recommendations` list (string array of specific, actionable improvements). The tool returns RAW data — the LLM is responsible for the rating + recommendations synthesis.

REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.""",
        tags=["templates", "workflow", "analysis", "health", "read-only", "optimization"],
        annotations=ToolAnnotations(
            title="Assess template health",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("assess_template_health")
    @handle_tallyfy_errors("assess template health")
    def assess_template_health(template_id: TemplateId) -> GenericDict:
        """
        Retrieve complete template data for health assessment.

        Args:
            template_id: Template ID to assess (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with full template data for comprehensive analysis
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            return ToolResult(
                content=serialize_dataclass(template),
                structured_content=None
            )

    @mcp.tool(
        name="update_template",
        description="""Update a template's metadata (title, summary, settings).

REQUIRED: 'template_id' (32-char hex) plus at least one property to update.

Updatable fields: title, summary, guidance, icon, alias, webhook, is_public, is_featured,
auto_naming, folderize_process, allow_launcher_change_name, is_pinned, default_folder,
kickoff_title, kickoff_description.

CORRECT usage:
  update_template(template_id="abc123...", title="New Template Name")
  update_template(template_id="abc123...", summary="Updated description", is_public=True)

Never call this without template_id.""",
        tags=["templates", "blueprints", "write", "management", "configuration"],
        annotations=ToolAnnotations(
            title="Update template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_template")
    @handle_tallyfy_errors("update template")
    def update_template(template_id: TemplateId, template_data: GenericDict) -> GenericDict:
        """
        Update a template's metadata and settings.

        Args:
            template_id: Template ID to update (REQUIRED - 32-character hex string)
            template_data: Dict of fields to update. Allowed keys: title, summary, guidance,
                icon, alias, webhook, is_public, is_featured, auto_naming, folderize_process,
                allow_launcher_change_name, is_pinned, default_folder, kickoff_title,
                kickoff_description (REQUIRED - must contain at least one field)

        Returns:
            Updated template object
        """
        if not template_data:
            raise ToolError("template_data must include at least one field to update (e.g. title, summary)")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Tallyfy API requires 'title' on every update request;
            # fetch the current title when the caller doesn't supply one.
            if "title" not in template_data:
                current = sdk.templates.get_template(org_id, template_id=template_id)
                if current and getattr(current, "title", None):
                    template_data["title"] = current.title

            result = sdk.templates.update_template_metadata(org_id, template_id, **template_data)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="clone_template",
        description="""Clone (duplicate) a template with a new name.

REQUIRED: 'template_id' (32-char hex) and 'new_name' (string).

CORRECT usage:
  clone_template(template_id="abc123...", new_name="Employee Onboarding v2")
  clone_template(template_id="abc123...", new_name="Copy of Template", copy_permissions=True)

Never call this without both required parameters.""",
        tags=["templates", "blueprints", "write", "management", "clone", "duplicate"],
        annotations=ToolAnnotations(
            title="Clone template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("clone_template")
    @handle_tallyfy_errors("clone template")
    def clone_template(
        template_id: TemplateId,
        new_name: str,
        copy_permissions: bool = False,
    ) -> GenericDict:
        """
        Clone (duplicate) a template with a new name.

        Args:
            template_id: Template ID to clone (REQUIRED - 32-character hex string)
            new_name: Name for the new template copy (REQUIRED)
            copy_permissions: Whether to copy template permissions to the new copy (default: False)

        Returns:
            New template object (the clone)
        """
        if not new_name or not new_name.strip():
            raise ToolError("new_name cannot be empty")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.duplicate_template(
                org_id, template_id, new_name.strip(),tenant=org_id,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="create_template",
        meta={
            "openai/toolInvocation/invoking": "Building your template...",
            "openai/toolInvocation/invoked": "Template created",
        },
        description="""Create a new template (checklist/blueprint). This is the first step when building a workflow from a user's description, uploaded document, or image — create the template shell here, then call add_step_to_template for each step, add_form_field_to_step for form fields, add_kickoff_field for pre-launch fields, and create_automation_rule for if-then logic.

REQUIRED: 'title' (template name). Optional: 'type' ('procedure' for multi-step workflows, 'form' for data collection, 'document' for reference docs), 'summary', 'guidance', 'starred'. Never call this without title.""",
        tags=["templates", "blueprints", "write", "create"],
        annotations=ToolAnnotations(
            title="Create template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_template")
    @handle_tallyfy_errors("create template")
    def create_template(
        title: TemplateTitle,
        type: OptionalString = "procedure",
        summary: OptionalString = None,
        guidance: OptionalString = None,
        starred: OptionalBool = None,
    ) -> GenericDict:
        """
        Create a new template.

        Args:
            title: Template title (REQUIRED)
            type: Template type ('procedure', 'form', 'document') (default: 'procedure')
            summary: Template description (optional)
            guidance: Guidance text for template users (optional)
            starred: Star the template (optional)

        Returns:
            Created template object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.create_template(
                org_id, title,
                type=type,
                summary=summary,
                guidance=guidance,
                starred=starred,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_template",
        description="Delete a template permanently. REQUIRED: 'template_id' (32-char hex). This action cannot be undone — all steps and automation rules will be deleted. Never call this without template_id.",
        tags=["templates", "blueprints", "write", "delete"],
        annotations=ToolAnnotations(
            title="Delete template",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_template")
    @handle_tallyfy_errors("delete template")
    def delete_template(template_id: TemplateId) -> GenericDict:
        """
        Delete a template permanently.

        Args:
            template_id: Template ID to delete (REQUIRED - 32-character hex string)

        Returns:
            Result of the deletion operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.delete_template(org_id, template_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_step",
        description="Delete a step from a template permanently. REQUIRED: 'template_id' (32-char hex) and 'step_id' (32-char hex). This action cannot be undone. Never call this without both parameters. NOTE: Deleting a step also removes any automation rules that reference it (as conditionable_id, target_step_id, or in then_actions); orphaned rules are pruned server-side. If you need to preserve those rules, update them to reference a different step BEFORE deleting.",
        tags=["templates", "steps", "write", "delete"],
        annotations=ToolAnnotations(
            title="Delete step",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_step")
    @handle_tallyfy_errors("delete step")
    def delete_step(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Delete a step from a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to delete (REQUIRED - 32-character hex string)

        Returns:
            Result of the deletion operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.delete_step(org_id, template_id, step_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="clone_step",
        description="Clone (duplicate) a step within a template. REQUIRED: 'template_id' (32-char hex) and 'step_id' (32-char hex). Creates an exact copy of the step including form fields and assignees. Never call this without both parameters.",
        tags=["templates", "steps", "write", "clone"],
        annotations=ToolAnnotations(
            title="Clone step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("clone_step")
    @handle_tallyfy_errors("clone step")
    def clone_step(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Clone (duplicate) a step within a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to clone (REQUIRED - 32-character hex string)

        Returns:
            New step object (the clone)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.clone_step(org_id, template_id, step_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="reorder_step",
        description="Move a step to a new position in a template. REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'position' (integer >= 0). Never call this without all three parameters.",
        tags=["templates", "steps", "write", "reorder"],
        annotations=ToolAnnotations(
            title="Reorder step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reorder_step")
    @handle_tallyfy_errors("reorder step")
    def reorder_step(
        template_id: TemplateId,
        step_id: StepId,
        position: StepPosition,
    ) -> GenericDict:
        """
        Move a step to a new position in a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to move (REQUIRED - 32-character hex string)
            position: New position for the step (REQUIRED - integer >= 0)

        Returns:
            Updated step object with new position
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.reorder_step(org_id, template_id, step_id, position)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )