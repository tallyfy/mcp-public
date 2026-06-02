"""
Form Field Management Tools
Tools for managing form fields in templates
"""

from typing import List, Union

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    TemplateId,
    StepId,
    FieldId,
    FieldIdList,
    GenericDict,
    GenericList,
    FieldPosition,
)
from utils.sdk_serializer import serialize_dataclass
from metrics import track_tool_execution


def register_form_fields_tools(mcp):
    """Register all form field management tools with the MCP server"""

    # Convenience aliases that map user-friendly field type names to the
    # canonical API field_type values.  Checked before the whitelist so
    # callers can use either the alias or the real name.
    _FIELD_TYPE_ALIASES = {
        "assignee": "assignees_form",
        "assignee_picker": "assignees_form",
        "member": "assignees_form",
        "member_picker": "assignees_form",
    }

    @mcp.tool(
        name="add_form_field_to_step",
        description="""Add form fields (text, dropdown, date, etc.) to a step.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'field_data' (dict).

SUPPORTED `field_type` ENUM (9 values — must be one of these exactly):
  - `text`           single-line text input
  - `textarea`       multi-line text input (free-form notes)
  - `date`           date picker (yyyy-mm-dd)
  - `dropdown`       single-select from a list of options
  - `multiselect`    multi-select from a list of options (checkbox-style)
  - `radio`          single-select radio buttons (mutually exclusive)
  - `file`           file upload
  - `table`          tabular data with named columns (REQUIRES `columns` list)
  - `assignees_form` member/assignee picker (the "person" field type)

CONVENIENCE ALIASES (auto-resolved): `assignee`, `assignee_picker`, `member`, `member_picker` → `assignees_form`.

CONDITIONAL REQUIRED `field_data` KEYS (per field_type):
  - `dropdown` / `radio` / `multiselect`: REQUIRE `options` — list of {text|label, optional id}
  - `table`: REQUIRES `columns` — list of {label, optional id} (id auto-assigned sequentially if omitted)

ALWAYS-REQUIRED `field_data` KEYS: `field_type`, `label`, `required` (bool).
Optional: `description`, `placeholder`, `validation`, `position`, `alias`.

CORRECT usage:
  add_form_field_to_step(template_id="abc...", step_id="def...",
    field_data={"field_type":"dropdown","label":"Priority","required":True,
                "options":[{"id":1,"text":"High"},{"id":2,"text":"Medium"},{"id":3,"text":"Low"}]})

  add_form_field_to_step(template_id="abc...", step_id="def...",
    field_data={"field_type":"text","label":"Customer Name","required":True})

Never call this without all three parameters.""",
        tags=["forms", "fields", "ui", "write", "management", "configuration"],
        annotations=ToolAnnotations(
            title="Add form field to step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_form_field_to_step")
    @handle_tallyfy_errors("add form field to step")
    def add_form_field_to_step(
        template_id: TemplateId,
        step_id: StepId,
        field_data: GenericDict,
    ) -> GenericDict:
        """
        Add form fields (text, dropdown, date, etc.) to a step.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_data: Form field creation data including field_type, label, required, etc. (REQUIRED)
                Supported field_type values:
                    text, textarea, date, dropdown, multiselect, radio,
                    file, table, assignees_form
                Convenience aliases (automatically resolved):
                    assignee -> assignees_form
                    assignee_picker -> assignees_form
                    member -> assignees_form
                    member_picker -> assignees_form

        Returns:
            Created form field object
        """
        # Resolve convenience aliases to canonical API field_type names
        ft = field_data.get("field_type", "")
        if ft and ft in _FIELD_TYPE_ALIASES:
            field_data["field_type"] = _FIELD_TYPE_ALIASES[ft]
            ft = field_data["field_type"]

        # Validate field_type against allowed values (per API's CaptureRequestValidator)
        allowed_field_types = {"text", "textarea", "date", "dropdown", "multiselect", "radio", "file", "table", "assignees_form"}
        if ft and ft not in allowed_field_types:
            raise ToolError(
                f"Invalid field_type '{ft}'. Allowed values: {', '.join(sorted(allowed_field_types))}"
            )

        # Validate conditional required fields
        if ft == "table" and not field_data.get("columns"):
            raise ToolError("field_type 'table' requires a 'columns' list in field_data")
        if ft in ("dropdown", "radio"):
            options = field_data.get("options")
            if options:
                for opt in options:
                    if isinstance(opt, dict) and "text" not in opt and "label" not in opt:
                        raise ToolError(
                            f"Each option for field_type '{ft}' must have a 'text' or 'label' field"
                        )

        # Auto-generate sequential IDs for dropdown/radio options that are missing them
        if 'options' in field_data and isinstance(field_data['options'], list):
            for i, option in enumerate(field_data['options'], start=1):
                if isinstance(option, dict) and 'id' not in option:
                    option['id'] = i

        # Auto-generate sequential IDs for table columns that are missing them
        if 'columns' in field_data and isinstance(field_data['columns'], list):
            for i, column in enumerate(field_data['columns'], start=1):
                if isinstance(column, dict) and 'id' not in column:
                    column['id'] = i

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.add_form_field_to_step(org_id, template_id, step_id, field_data)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_form_field",
        description="""Update form field properties, validation, or options on an existing form field.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), 'field_id' (32-char hex), and 'field_data' (dict).

UPDATABLE vs IMMUTABLE PROPERTIES:

  UPDATABLE:
    - `label`        human-readable name
    - `description`  help/instruction text
    - `placeholder`  placeholder shown in empty input
    - `required`     mandatory flag (bool)
    - `validation`   validation rules object
    - `position`     ordering within the step
    - `options`      (dropdown/radio/multiselect ONLY) — prefer `update_dropdown_options` for cleaner semantics
    - `columns`      (table fields ONLY)

  IMMUTABLE (CANNOT be changed — would corrupt existing data):
    - `field_type`   To change a field's type: `delete_form_field` then `add_form_field_to_step`.
                     WARNING: Deletion drops ALL collected data for that field.
    - `id`           Permanent.
    - `alias`        Set on creation; changing breaks automation rules and references.

  AUTO-FILLED: The API requires `label`, `field_type`, `required` on every update. If you
  omit any, this tool fetches the current field and fills them in. To update only the
  `description`, pass `field_data={"description":"..."}` — the others auto-fill.

field_type enum (reference — NOT updatable):
  text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form

CORRECT usage:
  update_form_field(template_id="abc...", step_id="def...", field_id="ghi...",
    field_data={"label":"Customer Name (legal)","required":True})

  # partial — field_type/required auto-filled from current state:
  update_form_field(template_id="abc...", step_id="def...", field_id="ghi...",
    field_data={"description":"Use the legal entity name from the contract"})

WRONG usage:
  field_data={"field_type":"textarea"}   # Cannot change type — delete + recreate instead

Never call this without all four parameters.""",
        tags=["forms", "fields", "ui", "write", "management", "configuration"],
        annotations=ToolAnnotations(
            title="Update form field",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_form_field")
    @handle_tallyfy_errors("update form field")
    def update_form_field(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId,
        field_data: GenericDict,
    ) -> GenericDict:
        """
        Update form field properties, validation, options.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID containing the field (REQUIRED - 32-character hex string)
            field_id: Form field ID to update (REQUIRED - 32-character hex string)
            field_data: Dictionary containing updated field properties (REQUIRED)
                - Can include: label, field_type, required, placeholder, options, etc.
                - Only specified fields will be updated

        Returns:
            Updated form field object data
        """
        if not field_data:
            raise ToolError("field_data must include at least one property to update (e.g. label, required, description)")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Tallyfy API requires 'label', 'field_type', and 'required' on every update;
            # it also requires 'options' for dropdown/radio/multiselect and 'columns' for
            # table fields (even on partial updates). Fetch the current field to fill in
            # any missing keys the API would reject without.
            required_keys = {"label", "field_type", "required"}
            missing_keys = required_keys - field_data.keys()

            # Determine field_type early to check conditional requirements
            ft = field_data.get("field_type")
            needs_options = ft in ("dropdown", "radio", "multiselect") and "options" not in field_data
            needs_columns = ft == "table" and "columns" not in field_data

            if missing_keys or needs_options or needs_columns:
                current = None

                # Try step captures first
                try:
                    step = sdk.form_fields.get_step(org_id, template_id, step_id)
                except Exception:
                    step = None
                if step and hasattr(step, "captures") and step.captures:
                    current = next(
                        (c for c in step.captures if getattr(c, "id", None) == field_id),
                        None,
                    )

                # Fallback: kickoff/prerun fields live on the template, not on a step
                if current is None:
                    try:
                        template = sdk.templates.get_template(org_id, template_id)
                    except Exception:
                        template = None
                    if template and hasattr(template, "prerun") and template.prerun:
                        current = next(
                            (f for f in template.prerun if getattr(f, "id", None) == field_id),
                            None,
                        )

                if current is None:
                    raise ToolError(
                        f"Cannot auto-fill required fields ({', '.join(sorted(missing_keys))}). "
                        f"Failed to fetch current field data for field '{field_id}' on step '{step_id}'. "
                        f"Please include 'label', 'field_type', and 'required' explicitly in field_data."
                    )

                for key in missing_keys:
                    default = False if key == "required" else None
                    field_data[key] = getattr(current, key, default)

                # Re-check field_type now that it may have been auto-filled
                ft = field_data.get("field_type")
                if ft in ("dropdown", "radio", "multiselect") and "options" not in field_data:
                    field_data["options"] = getattr(current, "options", []) or []
                if ft == "table" and "columns" not in field_data:
                    field_data["columns"] = getattr(current, "columns", []) or []

            result = sdk.form_fields.update_form_field(org_id, template_id, step_id, field_id, **field_data)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="move_form_field",
        description="Move form field between steps with position control. REQUIRED: 'template_id' (32-char hex), 'from_step' (32-char hex), 'field_id' (32-char hex), and 'to_step' (32-char hex). Optional: 'position' (defaults to 1). Never call this without the four required parameters.",
        tags=["forms", "fields", "ui", "write", "management", "positioning"],
        annotations=ToolAnnotations(
            title="Move form field",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("move_form_field")
    @handle_tallyfy_errors("move form field")
    def move_form_field(
        template_id: TemplateId,
        from_step: StepId,
        field_id: FieldId,
        to_step: StepId,
        position: FieldPosition = 1,
    ) -> bool:
        """
        Move form field between steps.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            from_step: Source step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID to move (REQUIRED - 32-character hex string)
            to_step: Target step ID (REQUIRED - 32-character hex string)
            position: Position in target step (default: 1)

        Returns:
            True if move was successful
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.move_form_field(org_id, template_id, from_step, field_id, to_step, position)
            if result:
                return ToolResult(
                    content={
                        "success": True,
                        "message": f"Form field '{field_id}' moved from step '{from_step}' to step '{to_step}' at position {position}.",
                        "field_id": field_id,
                        "from_step": from_step,
                        "to_step": to_step,
                        "position": position,
                    },
                    structured_content=None,
                )
            else:
                return ToolResult(
                    content={
                        "success": False,
                        "message": f"Move failed — field '{field_id}' was NOT moved to step '{to_step}'. The field may still be on the original step '{from_step}'. Verify with get_template_steps.",
                        "field_id": field_id,
                        "from_step": from_step,
                        "to_step": to_step,
                    },
                    structured_content=None,
                )

    @mcp.tool(
        name="delete_form_field",
        description="Delete a form field from a step. REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'field_id' (32-char hex). Never call this without all three parameters.",
        tags=["forms", "fields", "ui", "write", "management", "deletion"],
        annotations=ToolAnnotations(
            title="Delete form field",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_form_field")
    @handle_tallyfy_errors("delete form field")
    def delete_form_field(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId
    ) -> bool:
        """
        Delete a form field from a step.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID (REQUIRED - 32-character hex string)

        Returns:
            True if deletion was successful
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.delete_form_field(org_id, template_id, step_id, field_id)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="get_dropdown_options",
        description="Get current dropdown options for analysis. REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'field_id' (32-char hex). Never call this without all three parameters.",
        tags=["forms", "fields", "ui", "read-only", "dropdown", "options"],
        annotations=ToolAnnotations(
            title="Get dropdown options",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_dropdown_options")
    @handle_tallyfy_errors("get dropdown options")
    def get_dropdown_options(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId
    ) -> List[str]:
        """
        Get current dropdown options for analysis.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID (REQUIRED - 32-character hex string)

        Returns:
            List of dropdown option strings
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.get_dropdown_options(org_id, template_id, step_id, field_id)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="update_dropdown_options",
        description="""Replace the options list on a dropdown/radio/multiselect form field.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), 'field_id' (32-char hex), and 'options' (list).

ACCEPTED `options` FORMATS — pass EITHER format (the tool normalizes both):

  Format A — STRING ARRAY (simplest, recommended for new options):
    options=["High", "Medium", "Low"]
    The tool auto-converts each string to {id: <sequential>, label: <string>}.
    IDs are assigned 1, 2, 3, ... in input order.

  Format B — DICT ARRAY with explicit ids/labels:
    options=[
      {"id": 1, "label": "High"},
      {"id": 2, "label": "Medium"},
      {"id": 3, "label": "Low"}
    ]
    Use this when you need to preserve specific IDs (e.g. matching external
    system codes, or keeping IDs stable across updates).

  Format B alternate — `text` instead of `label` (also accepted):
    options=[{"id": 1, "text": "High"}, ...]

REPLACEMENT vs APPEND: This tool REPLACES the entire options list — it does NOT
append. To add an option, fetch existing options with `get_dropdown_options`,
add the new entry to the array, and pass the full updated list back.

CAUTION: Removing or changing the IDs of existing options can affect already-collected
form data (existing process runs reference options by ID). Prefer adding new options
with new IDs over deleting/renumbering existing ones.

CORRECT usage:
  update_dropdown_options(
    template_id="abc...", step_id="def...", field_id="ghi...",
    options=["Approved", "Rejected", "Needs Review"]
  )

  update_dropdown_options(
    template_id="abc...", step_id="def...", field_id="ghi...",
    options=[{"id": 100, "label": "Approved"}, {"id": 200, "label": "Rejected"}]
  )

Never call this without all four parameters.""",
        tags=["forms", "fields", "ui", "write", "dropdown", "options", "configuration"],
        annotations=ToolAnnotations(
            title="Update dropdown options",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_dropdown_options")
    @handle_tallyfy_errors("update dropdown options")
    def update_dropdown_options(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId,
        options: Union[List[str], List[GenericDict]],
    ) -> bool:
        """
        Update dropdown options (for external data integration).

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID (REQUIRED - 32-character hex string)
            options: List of option strings or dicts (REQUIRED).
                     Strings are auto-converted to {id, label} format.

        Returns:
            True if the update was successful
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.update_dropdown_options(org_id, template_id, step_id, field_id, options)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="suggest_form_fields_for_step",
        description="""Retrieve step details with existing form fields to recommend additional fields.

Returns the step's title, summary, type, and existing form fields (captures), plus the template title for context.

Use this data to suggest relevant form fields by considering:
- The step's purpose (inferred from title and summary)
- What fields already exist (avoid duplicates)
- Common patterns: approval steps need decision dropdowns, contact steps need text fields for email/phone,
  document steps need file uploads, financial steps need amount fields
- Supported field types: text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form

RECOMMENDATION OUTPUT: After analyzing the returned data, produce a list of recommended fields where each entry includes:
- field_type (one of: text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form)
- label (suggested human-readable label for the field)
- why (one-sentence rationale tying the suggestion back to step purpose, e.g. "approval steps need a decision dropdown so the assignee's verdict is captured structurally" or "contact-collection steps need a text field for email capture and downstream notifications")
- required (true/false based on whether the data is essential)

To add suggested fields, use add_form_field_to_step with the recommended field_data.

REQUIRED: 'template_id' (32-char hex) and 'step_id' (32-char hex). Never call this without both parameters.""",
        tags=["forms", "fields", "ui", "read-only", "suggestions", "analysis"],
        annotations=ToolAnnotations(
            title="Suggest form fields",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_form_fields_for_step")
    @handle_tallyfy_errors("suggest form fields for step")
    def suggest_form_fields_for_step(
        template_id: TemplateId,
        step_id: StepId
    ) -> GenericDict:
        """
        Retrieve step details with existing form fields for field suggestions.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with step data, existing fields, and template context
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

            existing_fields = [serialize_dataclass(c) for c in target_step.captures] if target_step.captures else []

            return ToolResult(
                content={
                    'step': serialize_dataclass(target_step),
                    'existing_fields': existing_fields,
                    'template_title': template.title,
                    'template_id': template_id,
                },
                structured_content=None
            )

    @mcp.tool(
        name="reorder_form_fields",
        description="Reorder form fields in a step. REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'field_order' (list of field IDs in desired order). Never call this without all three parameters.",
        tags=["forms", "fields", "write", "reorder"],
        annotations=ToolAnnotations(
            title="Reorder form fields",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reorder_form_fields")
    @handle_tallyfy_errors("reorder form fields")
    def reorder_form_fields(
        template_id: TemplateId,
        step_id: StepId,
        field_order: GenericList,
    ) -> GenericDict:
        """
        Reorder form fields in a step.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_order: List of field objects in the desired order (REQUIRED)

        Returns:
            Updated step with reordered fields
        """
        api_key, org_id = get_authenticated_credentials()
        # Normalize field_order items to use capture_id (API requirement).
        # Preserve position if provided — API requires both capture_id and position.
        normalized_order = []
        for i, item in enumerate(field_order, start=1):
            if isinstance(item, dict):
                field_id = item.get('id') or item.get('capture_id')
                normalized = {'capture_id': field_id, 'position': item.get('position', i)}
                normalized_order.append(normalized)
            elif isinstance(item, str):
                normalized_order.append({'capture_id': item, 'position': i})
            else:
                normalized_order.append(item)
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.reorder_form_fields(org_id, template_id, step_id, normalized_order)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    # ------------------------------------------------------------------
    # Kickoff / prerun field helpers
    # ------------------------------------------------------------------

    def _get_template_and_prerun(sdk, org_id, template_id):
        """Fetch template title and current kickoff fields as raw dicts.

        Uses ``get_template_with_steps`` which exposes ``raw_data`` — the
        unmodified API response before SDK model conversion.  This preserves
        ``label`` and every other field the API returns, which the
        ``PrerunField`` dataclass would otherwise drop.

        Returns (title, prerun_list).
        """
        result = sdk.templates.get_template_with_steps(org_id, template_id=template_id)
        if not result or not result.get("raw_data"):
            raise ToolError(f"Template '{template_id}' not found")
        raw = result["raw_data"]
        return raw.get("title", ""), raw.get("prerun", [])

    @mcp.tool(
        name="add_kickoff_field",
        description="""Add a form field to a template's kickoff (prerun) form.

Kickoff fields are filled out BEFORE a process starts — they collect initialization data
(e.g. customer name, start date, priority). This is different from step form fields which
are filled out DURING the process.

REQUIRED: 'template_id' (32-char hex) and 'field_data' (dict).

SUPPORTED `field_type` ENUM (same as step fields):
  text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form

CONVENIENCE ALIASES: assignee, assignee_picker, member, member_picker → assignees_form

CONDITIONAL REQUIRED `field_data` KEYS:
  - dropdown / radio / multiselect: REQUIRE `options` — list of {text, optional id}
  - table: REQUIRES `columns` — list of {label, optional id}

ALWAYS-REQUIRED `field_data` KEYS: `field_type`, `label`, `required` (bool).
Optional: guidance, position, collect_time, use_wysiwyg_editor, field_validation,
          default_value, default_value_enabled, prefix, suffix, settings.

CORRECT usage:
  add_kickoff_field(template_id="abc...",
    field_data={"field_type":"text","label":"Customer Name","required":true})

  add_kickoff_field(template_id="abc...",
    field_data={"field_type":"dropdown","label":"Priority","required":true,
                "options":[{"id":1,"text":"High"},{"id":2,"text":"Medium"},{"id":3,"text":"Low"}]})

Never call this without both parameters.""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "configuration"],
        annotations=ToolAnnotations(
            title="Add kickoff field",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_kickoff_field")
    @handle_tallyfy_errors("add kickoff field")
    def add_kickoff_field(
        template_id: TemplateId,
        field_data: GenericDict,
    ) -> GenericDict:
        """
        Add a form field to a template's kickoff (prerun) form.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_data: Field definition dict (REQUIRED) — must include field_type, label, required

        Returns:
            Updated template object with the new kickoff field
        """
        if not field_data.get("label"):
            raise ToolError("field_data must include 'label'")
        if not field_data.get("field_type"):
            raise ToolError("field_data must include 'field_type'")

        ft = field_data.get("field_type", "")
        if ft in _FIELD_TYPE_ALIASES:
            field_data["field_type"] = _FIELD_TYPE_ALIASES[ft]
            ft = field_data["field_type"]

        allowed_field_types = {"text", "textarea", "date", "dropdown", "multiselect", "radio", "file", "table", "assignees_form"}
        if ft not in allowed_field_types:
            raise ToolError(f"Invalid field_type '{ft}'. Allowed values: {', '.join(sorted(allowed_field_types))}")

        if ft == "table" and not field_data.get("columns"):
            raise ToolError("field_type 'table' requires a 'columns' list in field_data")
        if ft in ("dropdown", "radio", "multiselect") and not field_data.get("options"):
            raise ToolError(f"field_type '{ft}' requires an 'options' list in field_data")

        if "options" in field_data and isinstance(field_data["options"], list):
            for i, opt in enumerate(field_data["options"], start=1):
                if isinstance(opt, dict) and "id" not in opt:
                    opt["id"] = i

        if "columns" in field_data and isinstance(field_data["columns"], list):
            for i, col in enumerate(field_data["columns"], start=1):
                if isinstance(col, dict) and "id" not in col:
                    col["id"] = i

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            title, existing = _get_template_and_prerun(sdk, org_id, template_id)
            existing.append(field_data)
            result = sdk.update_template_metadata(
                org_id, template_id, title=title, prerun=existing,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_kickoff_field",
        description="""Update a kickoff (prerun) field on a template.

REQUIRED: 'template_id' (32-char hex), 'field_id' (32-char hex), and 'field_data' (dict).

UPDATABLE PROPERTIES:
  label, guidance, required (bool), position, options (dropdown/radio/multiselect),
  columns (table), collect_time, use_wysiwyg_editor, field_validation,
  default_value, default_value_enabled, prefix, suffix, settings.

IMMUTABLE: field_type, id, alias — cannot be changed (delete + recreate instead).

AUTO-FILLED: This tool fetches the current field and merges your changes in, so you
only need to pass the properties you want to change.

CORRECT usage:
  update_kickoff_field(template_id="abc...", field_id="def...",
    field_data={"required": true})

  update_kickoff_field(template_id="abc...", field_id="def...",
    field_data={"label": "Updated Name", "guidance": "Enter the legal entity name"})

Never call this without all three parameters.""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "configuration"],
        annotations=ToolAnnotations(
            title="Update kickoff field",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_kickoff_field")
    @handle_tallyfy_errors("update kickoff field")
    def update_kickoff_field(
        template_id: TemplateId,
        field_id: FieldId,
        field_data: GenericDict,
    ) -> GenericDict:
        """
        Update a kickoff (prerun) field on a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kickoff field ID to update (REQUIRED - 32-character hex string)
            field_data: Properties to update (REQUIRED)

        Returns:
            Updated template object
        """
        if not field_data:
            raise ToolError("field_data must include at least one property to update")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            title, existing = _get_template_and_prerun(sdk, org_id, template_id)

            target = None
            for f in existing:
                if f.get("id") == field_id:
                    target = f
                    break

            if target is None:
                raise ToolError(
                    f"Kickoff field '{field_id}' not found on template '{template_id}'. "
                    f"Use get_kickoff_fields to list available fields."
                )

            target.update(field_data)

            result = sdk.update_template_metadata(
                org_id, template_id, title=title, prerun=existing,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_kickoff_field",
        description="""Delete a kickoff (prerun) field from a template.

REQUIRED: 'template_id' (32-char hex) and 'field_id' (32-char hex).

WARNING: Deleting a kickoff field removes it permanently and drops all collected data
for that field across existing process runs. This cannot be undone.

NOTE: If the field is used in an automation rule (visibility condition), the deletion
will fail with a 403 error — remove the automation rule first.

Never call this without both parameters.""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "deletion"],
        annotations=ToolAnnotations(
            title="Delete kickoff field",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_kickoff_field")
    @handle_tallyfy_errors("delete kickoff field")
    def delete_kickoff_field(
        template_id: TemplateId,
        field_id: FieldId,
    ) -> GenericDict:
        """
        Delete a kickoff (prerun) field from a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kickoff field ID to delete (REQUIRED - 32-character hex string)

        Returns:
            Success status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            endpoint = f"organizations/{org_id}/checklists/{template_id}/preruns/{field_id}"
            result = sdk._make_request("DELETE", endpoint)
            return ToolResult(content=result or {"success": True}, structured_content=None)

    @mcp.tool(
        name="reorder_kickoff_fields",
        description="""Reorder kickoff (prerun) fields on a template.

REQUIRED: 'template_id' (32-char hex) and 'field_order' (list of field IDs in desired order).

The API assigns position sequentially based on array order — first ID gets position 1,
second gets position 2, etc. Fields not included in field_order are appended at the end
in their original relative order.

CORRECT usage:
  reorder_kickoff_fields(template_id="abc...",
    field_order=["field_id_3", "field_id_1", "field_id_2"])

Never call this without both parameters.""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "reorder"],
        annotations=ToolAnnotations(
            title="Reorder kickoff fields",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reorder_kickoff_fields")
    @handle_tallyfy_errors("reorder kickoff fields")
    def reorder_kickoff_fields(
        template_id: TemplateId,
        field_order: FieldIdList,
    ) -> GenericDict:
        """
        Reorder kickoff (prerun) fields on a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_order: List of field IDs in desired order (REQUIRED)

        Returns:
            Updated template object with reordered kickoff fields
        """
        if not field_order:
            raise ToolError("field_order must be a non-empty list of field IDs")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            title, existing = _get_template_and_prerun(sdk, org_id, template_id)

            fields_by_id = {f.get("id"): f for f in existing if f.get("id")}

            # Update each item's `position` field as we rebuild the list — Tallyfy's
            # API derives display order from the `position` integer on each prerun
            # item, NOT from array index alone. The sibling `reorder_form_fields`
            # tool above writes explicit `{capture_id, position}` pairs for the same
            # reason. BugBot finding on PR #496: "reorders array but ignores position
            # field on items; UI may still show old order" — addressed by writing
            # positions explicitly here.
            reordered = []
            for pos, fid in enumerate(field_order, start=1):
                if fid in fields_by_id:
                    item = fields_by_id.pop(fid)
                    item["position"] = pos
                    reordered.append(item)
                else:
                    raise ToolError(
                        f"Field '{fid}' not found in template kickoff fields. "
                        f"Use get_kickoff_fields to list available fields."
                    )

            # Append any fields the caller omitted from field_order, preserving their
            # relative order and continuing the position sequence.
            next_pos = len(reordered) + 1
            for f in existing:
                if f.get("id") in fields_by_id:
                    f["position"] = next_pos
                    reordered.append(f)
                    next_pos += 1

            result = sdk.update_template_metadata(
                org_id, template_id, title=title, prerun=reordered,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_kickoff_dropdown_options",
        description="""Get dropdown/radio/multiselect options for a kickoff (prerun) field.

REQUIRED: 'template_id' (32-char hex) and 'field_id' (32-char hex).

Returns the options array for the specified kickoff field. Only works on fields with
field_type dropdown, radio, or multiselect — returns an error for other field types.

CORRECT usage:
  get_kickoff_dropdown_options(template_id="abc...", field_id="def...")

Never call this without both parameters.""",
        tags=["forms", "fields", "kickoff", "prerun", "read-only", "dropdown", "options"],
        annotations=ToolAnnotations(
            title="Get kickoff dropdown options",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_kickoff_dropdown_options")
    @handle_tallyfy_errors("get kickoff dropdown options")
    def get_kickoff_dropdown_options(
        template_id: TemplateId,
        field_id: FieldId,
    ) -> GenericDict:
        """
        Get dropdown/radio/multiselect options for a kickoff (prerun) field.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kickoff field ID (REQUIRED - 32-character hex string)

        Returns:
            Options list for the specified field
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            _, existing = _get_template_and_prerun(sdk, org_id, template_id)

            target = None
            for f in existing:
                if f.get("id") == field_id:
                    target = f
                    break

            if target is None:
                raise ToolError(
                    f"Kickoff field '{field_id}' not found on template '{template_id}'. "
                    f"Use get_kickoff_fields to list available fields."
                )

            ft = target.get("field_type", "")
            if ft not in ("dropdown", "radio", "multiselect"):
                raise ToolError(
                    f"Field '{field_id}' has field_type '{ft}' — options are only "
                    f"available for dropdown, radio, or multiselect fields."
                )

            return ToolResult(
                content=target.get("options", []),
                structured_content=None
            )

