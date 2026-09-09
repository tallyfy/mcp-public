"""Mermaid rendering of a template's visibility graph (spec section 9).

Keep the diagram SECONDARY. The sentence in the finding is the product; the
picture is there so a reader can see the dead branch at a glance. Findings are
STYLED, never written as prose inside a node.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from template_testing.document import CONDITIONABLE_PRERUN, TemplateDoc, parse_template


def _node(index: int) -> str:
    return f"S{index}"


def _escape(text: str) -> str:
    """Quotes and brackets end a node label early, so strip them.

    A step titled ``Review "final" [draft]`` renders as a syntax error otherwise,
    and a broken diagram is worse than a plain one.
    """
    return (
        str(text)
        .replace('"', "'")
        .replace("[", "(")
        .replace("]", ")")
        .replace("\n", " ")
        .strip()
    )


def _edge_label(doc: TemplateDoc, rule) -> str:
    parts: List[str] = []
    for cond in rule.conditions[:2]:
        if cond.conditionable_type == CONDITIONABLE_PRERUN or cond.conditionable_type == "Capture":
            fdef = doc.field(cond.conditionable_id)
            name = fdef.label if fdef else cond.conditionable_id
            value = cond.statement if cond.statement not in (None, "") else ""
            parts.append(f"{name} {cond.operation.replace('_', ' ')} {value}".strip())
        else:
            step = doc.step(cond.conditionable_id)
            name = step.title if step else cond.conditionable_id
            parts.append(f"{name} {cond.operation}")
    if len(rule.conditions) > 2:
        parts.append("...")
    return _escape(" / ".join(parts)) or "rule"


def render_mermaid(raw: Dict[str, Any], dead_step_ids: Optional[Iterable[str]] = None) -> str:
    """One node per step, labelled ``position. title``.

    A SOLID edge is "completing this shows that". A DOTTED edge is a hide, or a
    step nothing ever shows. Unreachable steps carry the ``dead`` class, which is
    the whole point of the picture.
    """
    doc = parse_template(raw)
    dead: Set[str] = set(dead_step_ids or ())
    index_of = {step.id: i + 1 for i, step in enumerate(doc.steps)}

    lines: List[str] = ["flowchart TD", "    Start([Process starts])"]
    for step in doc.steps:
        lines.append(
            f'    {_node(index_of[step.id])}["{_escape(f"{step.position}. {step.title}")}"]'
        )
    lines.append("")

    hidden = doc.hidden_at_launch()
    visible_ordered = [s for s in doc.steps if s.id not in hidden]
    if visible_ordered:
        lines.append(f"    Start --> {_node(index_of[visible_ordered[0].id])}")
        for left, right in zip(visible_ordered, visible_ordered[1:]):
            lines.append(
                f"    {_node(index_of[left.id])} --> {_node(index_of[right.id])}"
            )

    reached: Set[str] = set()
    for rule in doc.automations:
        if rule.archived_at:
            continue
        label = _edge_label(doc, rule)
        sources = [
            c.conditionable_id
            for c in rule.conditions
            if c.conditionable_type == "Step" and c.conditionable_id in index_of
        ]
        for act in rule.actions:
            if act.action_type != "visibility" or act.target_step_id not in index_of:
                continue
            target = _node(index_of[act.target_step_id])
            # A labelled mermaid edge is `A -- "text" --> B` when solid and
            # `A -. "text" .-> B` when dotted. Slicing an unlabelled arrow string
            # to build these produces `-- "text" ->`, which mermaid rejects, so
            # the two halves are written out rather than derived.
            if act.action_verb == "show":
                head, tail = "--", "-->"
                reached.add(act.target_step_id)
            else:
                head, tail = "-.", ".->"
            origin = _node(index_of[sources[0]]) if sources else "Start"
            lines.append(f'    {origin} {head} "{label}" {tail} {target}')

    for step_id in sorted(dead, key=lambda s: index_of.get(s, 0)):
        if step_id in index_of and step_id not in reached:
            lines.append(
                f'    Start -. "no rule ever shows this" .-> {_node(index_of[step_id])}'
            )

    lines.append("")
    lines.append(
        "    classDef dead fill:#fde8e8,stroke:#c81e1e,stroke-width:2px,color:#7f1d1d;"
    )
    styled = [_node(index_of[s]) for s in dead if s in index_of]
    if styled:
        lines.append(f"    class {','.join(styled)} dead;")
    return "\n".join(lines)
