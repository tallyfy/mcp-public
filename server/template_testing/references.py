"""Dangling-reference detection, shared with ``server/tools/automation.py``.

Spec check 6 says to REUSE the orphan block in ``_build_suggestions`` rather than
rewriting it, because it already encodes a guard that was learned the hard way and
it is covered by tests. It could not be imported: it was defined inside
``register_automation_tools``. So the id comparison moved here and both callers
use it, which is the only arrangement in which the two cannot drift.

🔴 The guard: a ``conditionable_id`` is a STEP id only when ``conditionable_type``
is ``Step``. api-v2 allows ``Step``, ``Capture`` and ``Prerun``
(AutomatedActionRequest.php:21) and the transformer emits ``conditionable_id`` for
all three. Validating every id against the step ids flags EVERY form-field and
kick-off-triggered rule as orphaned and recommends deleting working automations.
That regression shipped once, as mcp#617.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

# When a type's id set cannot be enumerated the type is SKIPPED rather than
# flagged, so a valid rule is never reported as an orphan on the strength of a
# read that did not happen.
UNAVAILABLE = None


def orphaned_reference_ids(
    conditions: Iterable[Dict[str, Any]],
    actions: Iterable[Dict[str, Any]],
    condition_id_sets: Dict[str, Optional[Set[str]]],
    valid_step_ids: Set[str],
) -> List[str]:
    """Ids one rule names that exist nowhere in the template.

    ``condition_id_sets`` maps a short ``conditionable_type`` to the ids valid for
    THAT type, or to ``None`` when they cannot be enumerated.

    A ``then_action``'s ``target_step_id`` is always a step id
    (AutomatedActionRequest.php:34, ``exists:steps,timeline_id``), so it is the one
    reference that reliably dangles.
    """
    orphaned: List[str] = []
    for cond in conditions or []:
        cid = cond.get("conditionable_id", "")
        if not cid:
            continue
        # api-v2 defaults an omitted conditionable_type to Step nowhere, but every
        # real condition carries one; fall back to Step defensively.
        ctype = cond.get("conditionable_type") or "Step"
        valid_ids = condition_id_sets.get(ctype)
        if valid_ids is not UNAVAILABLE and cid not in valid_ids:
            orphaned.append(cid)
    for act in actions or []:
        tid = act.get("target_step_id", "")
        if tid and tid not in valid_step_ids:
            orphaned.append(tid)
    return orphaned
