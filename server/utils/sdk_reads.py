"""Read an SDK model's field honestly: None is "not read", never "empty".

From SDK 4.0 (tallyfy/sdk PR #267) a model invents nothing. A key api-v2 did
not send is ``None``, where SDK 3.x filled ``[]``, ``0``, ``False`` or a
default string. For a key api-v2's transformer always sends (a template's
``steps``, ``prerun`` and ``automated_actions``, a step's ``captures``),
``None`` therefore means the read did not carry it, and reading it as an empty
list would report "this template has no steps" or "no kickoff fields" about a
template that has them (tallyfy/mcp#1545).

``required_list`` is the one place that turns such a ``None`` into a refusal,
so every tool says the same thing and classes it the same way: the read was
answered without what api-v2 always sends, which is ``upstream_error``, the
class ``utils.fastmcp_errors`` gives the SDK's own
``TallyfyUnexpectedResponseError``.
"""

from typing import Any, List

from fastmcp.exceptions import ToolError

from utils.fastmcp_errors import ERROR_CLASS_UPSTREAM_ERROR, tag_error_class


def required_list(value: Any, what: str) -> List[Any]:
    """``value`` as a list, or a ToolError when the read did not carry it.

    ``what`` names the thing for the caller, for example
    ``"the steps of template <id>"``. An empty list is a real answer and is
    returned as it is; only ``None`` (or a value that is not a list) refuses.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    raise tag_error_class(
        ToolError(
            f"Tallyfy's answer did not include {what}, so this tool cannot tell "
            f"them apart from none at all. Nothing was assumed and nothing was "
            f"changed. Try again."
        ),
        ERROR_CLASS_UPSTREAM_ERROR,
    )
