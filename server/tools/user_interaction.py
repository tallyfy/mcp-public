"""
User Interaction Tools
Tools for asking structured questions, ranking choices, and binary
confirmations during conversations. Each tool returns a description of the
form it wants shown; it does not show anything itself:

  ask_user_question   -> fields, submit_label        -> tf-question-interaction
  ask_user_to_rank    -> interaction_type "ranking"  -> tf-ranking-interaction
  ask_user_to_confirm -> interaction_type "confirm"  -> tf-confirm-interaction

Only our own chat host draws these forms. It recognises the tool NAME
(host/client/interaction_payload.py, INTERACTION_TOOLS), pauses the turn, and
forwards the person's answer as their next message. Every other MCP client
receives the description as an ordinary tool result and shows no form, so each
result carries ``status`` and ``note`` saying that nobody has answered yet
(#1241). Before that, an external client read the result as a successful
answer that no person had given.

The ``__structured_question__`` / ``__structured_interaction__`` keys these
results used to carry were removed in #1241: nothing in the host or the client
read them.
"""

from typing import List, Dict, Any, Optional
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from metrics import track_tool_execution

#: Sent with every result of the three tools. An MCP client that does not draw
#: forms would otherwise read the result as the person's answer (#1241).
AWAITING_REPLY_STATUS = "awaiting_user_reply"
AWAITING_REPLY_NOTE = (
    "Not answered yet. This result describes the question; it is not the "
    "user's reply. The user's answer arrives as their next message. Do not act "
    "on the question until it does, and make sure your reply tells the user "
    "what you are asking."
)


def register_user_interaction_tools(mcp):
    """Register user interaction tools with the MCP server."""

    @mcp.tool(
        name="ask_user_question",
        description=(
            "Ask the user a structured question with form fields. "
            "Use when you need clarification, confirmation, or user input "
            "before proceeding. Returns a structured question that the UI "
            "renders as a form. The conversation pauses until the user responds.\n\n"
            "RESPONSE FORMAT: When the user submits the form, the host forwards "
            "their answers back to the agent as a synthetic user turn shaped like "
            "{submitted: bool, fields: {<field_label>: <user_answer>}} where each "
            "field_answer is typed per the field's `type`: string for text/textarea/"
            "select/radio, bool for checkbox/toggle, ISO-8601 string for date/datetime/"
            "timepicker, {start, end} object for daterange, or a file_id reference "
            "for file. If the user dismisses the form, `submitted` is false and `fields` "
            "is empty. The synthetic turn arrives in the conversation log as "
            "`[Answers to prior question (question_id=...)] key=value, key2=value2`."
        ),
        tags=["interaction", "question", "form", "user-input"],
        annotations=ToolAnnotations(
            title="Ask user a structured question",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        output_schema=None
    )
    @track_tool_execution("ask_user_question")
    def ask_user_question(
        question: str,
        fields: List[Dict[str, Any]],
        submit_label: str = "Submit",
        header: str = "",
    ) -> Dict[str, Any]:
        """
        Present a structured question to the user with form fields.
        The conversation pauses until the user submits their response.

        Use this when:
        - Multiple valid interpretations exist and you need clarification
        - Creating/launching processes and need user to fill form fields
        - Parameters are ambiguous (e.g., multiple templates match a name)
        - You need user confirmation before a destructive action

        Args:
            question: The question text to display to the user
            fields: Array of form field configurations. Each field has:
                - type: "text"|"textarea"|"select"|"checkbox"|"toggle"|"radio"|
                        "datepicker"|"daterange"|"timepicker"|"datetime"|"file"
                - key: Unique field identifier (used as key in response data)
                - label: Display label for the field
                - required: Whether the field is required (default: false)
                - options: For select/radio, a list of {label, value, description?}
                - placeholder: Placeholder text for text inputs
                - validators: {required?, minLength?, maxLength?, pattern?}
                - visible: Whether to show the field (default: true)
                - gridColumn: Layout column span 1-12 (default: 12)
                - defaultValue: Default value for the field
            submit_label: Label for the submit button (default: "Submit")
            header: Short header/category label, max 12 chars (e.g., "Confirm", "Select")

        Returns:
            The question and its fields, plus ``status`` and ``note`` saying
            nobody has answered yet. Our chat host renders it as a form and
            pauses the conversation; other clients show no form.
        """
        # Validate fields have required properties
        validated_fields = []
        if fields:
            for i, field in enumerate(fields):
                if not isinstance(field, dict):
                    continue
                if "key" not in field or "type" not in field:
                    continue
                # Ensure label exists
                if "label" not in field:
                    field["label"] = str(field.get("key", "")).replace("_", " ").title()
                validated_fields.append(field)

        return ToolResult(
            content={
                "question": question,
                "header": header[:12] if header else "",
                "fields": validated_fields,
                "submit_label": submit_label or "Submit",
                "status": AWAITING_REPLY_STATUS,
                "note": AWAITING_REPLY_NOTE,
            },
            structured_content=None
        )

    @mcp.tool(
        name="ask_user_to_rank",
        description=(
            "Ask the user to drag-rank a list of options into a preferred "
            "order. Use when the agent has a candidate list (templates, "
            "tasks, options) whose ordering depends on the user's intent "
            "and a free-text answer would be lossy.\n\n"
            "RESPONSE FORMAT: When the user submits, the host forwards their "
            "ranked order back as a synthetic turn shaped like "
            "[Ranking answer (interaction_id=...) interaction_type=ranking] "
            "order=[id1, id2, id3]. The order reflects the user's drag "
            "arrangement; the items themselves are unchanged. If the user "
            "dismisses the form, a cancel_interaction frame fires and the "
            "agent receives [User cancelled prior interaction (interaction_id=...)]."
        ),
        tags=["interaction", "ranking", "user-input"],
        annotations=ToolAnnotations(
            title="Ask user to rank options",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        output_schema=None,
    )
    @track_tool_execution("ask_user_to_rank")
    def ask_user_to_rank(
        question: str,
        items: List[Dict[str, Any]],
        submit_label: str = "Submit",
        header: str = "",
        cancelable: bool = True,
    ) -> Dict[str, Any]:
        """Present a drag-rank interaction to the user.

        The conversation pauses until the user submits the reordered list
        OR dismisses the form. Renders client-side as
        ``tf-ranking-interaction`` via the W22 generic interaction block.

        Args:
            question: The question text to display above the list
            items: Array of items to rank. Each item is ``{id, label,
                description?}``. ``id`` is what comes back in the
                submission's ``order`` array; ``label`` is what the user
                sees on the drag handle.
            submit_label: Label for the submit button (default "Submit")
            header: Short category label, max 12 chars (e.g. "Rank")
            cancelable: When False, hides the cancel button and forces
                the user to submit. Default True.

        Returns:
            Interaction object with ``interaction_type='ranking'``, plus
            ``status`` and ``note`` saying nobody has answered yet. Our chat
            host renders it as a drag-orderable list and the conversation
            pauses until the user submits or cancels.
        """
        # Validate items have required properties — drop bad entries silently
        validated_items: List[Dict[str, Any]] = []
        if items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "id" not in item:
                    continue
                if "label" not in item:
                    item["label"] = str(item["id"])
                validated_items.append(item)

        return ToolResult(
            content={
                "interaction_type": "ranking",
                "question": question,
                "header": header[:12] if header else "",
                "schema": {"items": validated_items},
                "submit_label": submit_label or "Submit",
                "cancelable": bool(cancelable),
                "status": AWAITING_REPLY_STATUS,
                "note": AWAITING_REPLY_NOTE,
            },
            structured_content=None,
        )

    @mcp.tool(
        name="ask_user_to_confirm",
        description=(
            "Ask the user to confirm or decline a single binary choice. "
            "Use before any destructive or irreversible action (deleting a "
            "template, archiving a process, sending an invitation), or "
            "when an action's effect is non-obvious and the user should "
            "explicitly opt in.\n\n"
            "RESPONSE FORMAT: When the user submits, the host forwards their "
            "decision as a synthetic turn shaped like "
            "[Confirmation answer (interaction_id=...) interaction_type=confirm] "
            "confirmed=true|false. If the user dismisses the form, a "
            "cancel_interaction frame fires and the agent receives "
            "[User cancelled prior interaction (interaction_id=...)]."
        ),
        tags=["interaction", "confirmation", "user-input"],
        annotations=ToolAnnotations(
            title="Ask user to confirm an action",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        output_schema=None,
    )
    @track_tool_execution("ask_user_to_confirm")
    def ask_user_to_confirm(
        question: str,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        header: str = "",
        cancelable: bool = True,
    ) -> Dict[str, Any]:
        """Present a binary confirm/decline interaction.

        The conversation pauses until the user submits a decision or
        dismisses the form. Renders client-side as
        ``tf-confirm-interaction`` via the W22 generic interaction block.

        Args:
            question: The question text (e.g. "Delete template 'Onboarding'?")
            confirm_label: Affirmative button label (default "Confirm")
            cancel_label: Negative button label (default "Cancel"). When
                ``cancelable`` is False, the cancel button is hidden
                regardless of this value.
            header: Short category label, max 12 chars (e.g. "Confirm")
            cancelable: When False, the user must confirm. There is no decline
                path. Default True. Use sparingly: forcing confirmation
                without a decline option is a UX anti-pattern except when
                the agent's earlier turn explicitly framed the prompt as
                "click to acknowledge".

        Returns:
            Interaction object with ``interaction_type='confirm'``, plus
            ``status`` and ``note`` saying nobody has answered yet. Our chat
            host renders it as a confirm/cancel button pair.
        """
        return ToolResult(
            content={
                "interaction_type": "confirm",
                "question": question,
                "header": header[:12] if header else "",
                "schema": {
                    "confirm_label": confirm_label or "Confirm",
                    "cancel_label": cancel_label or "Cancel",
                },
                "cancelable": bool(cancelable),
                "status": AWAITING_REPLY_STATUS,
                "note": AWAITING_REPLY_NOTE,
            },
            structured_content=None,
        )
