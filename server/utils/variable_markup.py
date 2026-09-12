"""The Tallyfy rich-text embed contract, published to models (#1292).

WHY THIS EXISTS
---------------
A customer asked Claude to write template steps referencing a form field. Claude
wrote ``{{alias}}`` as plain text. That resolves at runtime, so it looked right,
but in the editor it is dead black text rather than a blue chip, and the customer
had to relink three of them by hand on a call.

Claude had no way to know better. Measured 2026-09-11 across the whole tools
tree: the Tallyfy variable syntax appeared ZERO times in any tool description or
docstring. The only acknowledgement anywhere was
``utils/field_value_encoding.py``, warning that a value renders blank when USED
as a variable, which is about breaking one rather than writing one. Worse, the
word ``alias`` is actively taught as a trap by ``CLAUDE.md`` rule 1 item 3
(using it for a prerun VALUE silently discards the value), so a model is warned
off the exact word that identifies a variable.

WHERE THIS COMES FROM, AND WHY IT IS NOT A COPY OF THE ISSUE
-------------------------------------------------------------
Read from the CONSUMER, which is the only thing that decides whether markup a
model writes actually renders. ``client-v2`` resolves embeds in
``src/app/directives/html-view.directive.ts``:

* variables      -> ``element.querySelectorAll('.insert-variable-tag')`` (:578, :606)
                    then matches ``f"{{{{{field['alias']}}}}}" == tag.dataset.variableId``
                    (:619), where ``variableId`` is copied from the span's own
                    textContent at render time (:581). So the STORED form is a
                    span carrying the class, whose TEXT is ``{{alias}}``.
* snippets       -> ``'editor-snippet, app-editor-snippet, .insert-snippet-tag'`` (:280)
                    and the id is ``dataset.snippetId``, stripped of quotes and
                    required to be a positive integer (:290).
* blueprints     -> ``'editor-blueprint, app-editor-blueprint, .insert-blueprint-tag'`` (:390)
* document fields-> ``'editor-form-field, app-editor-form-field, editor-variable, ...'``
                    and the id is ``readChipAttr(host, 'data-field-id')`` (:498)
* mentions       -> the ``@[id]`` token, stored as a token and not as display
                    HTML (``MentionTransformerService``)

🔴 **THE CLASS IS THE SELECTOR. An attribute alone does not render.** A span
carrying ``data-snippet-id`` but no ``insert-snippet-tag`` class matches no
query in that directive and is inert, and the same is true of a variable span
with no ``insert-variable-tag`` class. This is the single most important fact
here and it is the one an attribute-only spec leaves out.

⚠️ **Two things commonly stated about this markup were NOT reproducible and are
deliberately not published.** Recording them so nobody re-adds them from a stale
note:

* ``fr-deletable`` is a LEGACY Froala class. It appears in content authored by
  the old editor (api-v2's own fixture at
  ``internal-assets/db-schema-data-minimal.sql`` stores
  ``class="fr-deletable insert-variable-tag"``), and client-v2's round-trip spec
  (``rich-text-field.round-trip.spec.ts``) pins the chip WITHOUT it. It is
  harmless if present and is not required, so it is omitted rather than taught.
* **U+FEFF sentinels around the span could not be confirmed anywhere.** Neither
  the api-v2 stored fixture nor the client-v2 spec carries one, and no selector
  in the directive depends on one. They are therefore not published. If somebody
  pins a source that requires them, add it here with the citation rather than
  re-deriving this paragraph.

TWO FORMATS, AND A MODEL MUST NOT MIX THEM
-------------------------------------------
Format 1 is the rich-text surface (step instructions, process description, a
WYSIWYG field's default content) and uses ``<span class="insert-*-tag">``.
Format 2 is the document editor and uses custom element tags
(``<editor-snippet>``, ``<editor-blueprint>``, ``<editor-form-field>``). The
directive matches both so a preview renders either, but content authored through
these tools is Format 1, so Format 1 is what the tools teach.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple

#: The MCP resource that serves the full contract.
VARIABLE_MARKUP_URI = "tallyfy://variable-markup"

#: The pointer every description-writing tool carries.
#:
#: It is ONE line on purpose. Measured 2026-09-11, the seven tools that write a
#: description have between 71 and 1340 bytes of headroom under the hard
#: 2000-BYTE tool-description cap, and the tightest two (``create_standalone_task``
#: at 71 and ``add_step_to_template`` at 72) cannot carry a paragraph. The shape
#: lives in the resource and in the JSON schema, where neither costs a byte of
#: that cap.
MARKUP_POINTER = "Field variables in HTML: read " + VARIABLE_MARKUP_URI + "."


class Embed(NamedTuple):
    """One embed type, as it must be STORED."""

    name: str
    markup: str
    identifier: str
    where_to_get_it: str
    note: str


#: Order is deliberate: the variable is the one the customer hit, so it is first.
EMBEDS: List[Embed] = [
    Embed(
        name="form-field variable",
        markup='<span class="insert-variable-tag" contenteditable="false">{{alias}}</span>',
        identifier="the field's `alias`",
        where_to_get_it=(
            "get_kickoff_fields(template_id) for a kickoff field, or "
            "get_template_steps(template_id) for a step field, which returns "
            "each step with its `captures`. Use the `alias`, NOT the id and "
            "NOT the label."
        ),
        note=(
            "Writing a bare {{alias}} with no span resolves at runtime but shows "
            "as dead text in the editor instead of a chip, and a human has to "
            "relink it by hand. The class is what the renderer queries on."
        ),
    ),
    Embed(
        name="snippet",
        markup=(
            '<span class="insert-snippet-tag" contenteditable="false" '
            'data-snippet-id="12">[[Snippet title]]</span>'
        ),
        identifier="a text_templates id (a positive INTEGER)",
        where_to_get_it="search_for_snippets",
        note=(
            "The id is read as an integer and anything else is dropped, so a "
            "32-hex id here is silently ignored."
        ),
    ),
    Embed(
        name="embedded blueprint",
        markup=(
            '<span class="insert-blueprint-tag" '
            'data-blueprint-id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"></span>'
        ),
        identifier="a checklists (template) id, 32-char hex",
        where_to_get_it="search_for_templates",
        note="Renders the whole referenced template inline, read-only.",
    ),
    Embed(
        name="document-mode form field",
        markup=(
            "<editor-form-field data-field-id=\"'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'\">"
            "</editor-form-field>"
        ),
        identifier="the field's `timeline_id`",
        where_to_get_it="get_kickoff_fields / get_template_steps",
        note=(
            "DOCUMENT-type templates only, and it is keyed on timeline_id, NOT "
            "on id. The surrounding single quotes are tolerated and are how the "
            "editor writes it."
        ),
    ),
    Embed(
        name="mention",
        markup="@[12345]",
        identifier="a numeric member id",
        where_to_get_it="get_organization_users",
        note=(
            "Plain text, not HTML, and not wrapped in a span. This is how a "
            "mention is STORED; the display name is resolved on render."
        ),
    ),
]


def _embed_block(embed: Embed) -> str:
    return (
        f"### {embed.name}\n"
        f"{embed.markup}\n"
        f"  identifier: {embed.identifier}\n"
        f"  get it from: {embed.where_to_get_it}\n"
        f"  note: {embed.note}\n"
    )


def variable_markup_reference() -> str:
    """The full contract, served as an MCP resource.

    Built from ``EMBEDS`` rather than written out, so the resource, the JSON
    schema help and any future surface cannot drift apart. A hand-maintained
    second copy is how this repo ended up advertising 110 tools while serving
    108.
    """
    body = "\n".join(_embed_block(e) for e in EMBEDS)
    return (
        "# Embedding fields, snippets and blueprints in Tallyfy rich text\n\n"
        "Step instructions, process descriptions and a WYSIWYG field's default\n"
        "content are HTML. To reference something you must write the markup\n"
        "below, EXACTLY. Writing the bare token instead still resolves when the\n"
        "text is rendered, but it shows in the editor as dead text rather than a\n"
        "chip, and somebody has to relink it by hand.\n\n"
        "THE CLASS IS WHAT MAKES IT WORK. The client finds every embed by a CSS\n"
        "selector on the class, then reads the identifier. An attribute on its\n"
        "own renders nothing.\n\n"
        + body
        + "\n## Worked example\n\n"
        'add_step_to_template(template_id="...", step_data={\n'
        '  "title": "Call the customer",\n'
        '  "summary": "<p>Call <span class=\\"insert-variable-tag\\" '
        'contenteditable=\\"false\\">{{customer-name-2451}}</span> today.</p>"\n'
        "})\n\n"
        "## Reading an existing one to copy\n\n"
        "get_template_steps returns each step's `summary` with this markup\n"
        "intact: nothing strips or rewrites it. A summary longer than the\n"
        "per-string cap is shortened and SAYS SO in the value itself; re-read\n"
        "that one step with get_template_steps(step_id=..., full_text=True)\n"
        "before copying or writing it back, or you will truncate the markup.\n"
    )


def summary_field_help() -> str:
    """Compact guidance for a JSON-schema field description.

    A PARAMETER description is published in the tool's JSON schema and does NOT
    count against the 2000-byte tool-description cap (``CLAUDE.md`` rule 34), so
    the shape can live here in full while the description carries one line.
    """
    variable = EMBEDS[0].markup
    return (
        "HTML instructions for the assignee. To reference a form field write "
        f"{variable} using the field's alias, not a bare {{{{alias}}}}, which "
        "shows as dead text in the editor. Snippets, blueprints, document "
        f"fields and mentions have their own markup: read {VARIABLE_MARKUP_URI}."
    )


def markup_help_by_embed() -> Dict[str, str]:
    """``{embed name: markup}``, for a caller that wants just the shapes."""
    return {e.name: e.markup for e in EMBEDS}
