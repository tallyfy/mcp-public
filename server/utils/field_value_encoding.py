"""
Per-type coercion for Tallyfy form-field values (kickoff `prerun` and step `taskdata`).

`utils/kickoff_encoding.py` normalizes the CONTAINER (list -> ID-keyed object).
This module normalizes the VALUES inside it, which is a separate failure mode:
a correctly-keyed payload carrying the wrong per-type value shape is rejected by
`api-v2/app/Http/Requests/Captures/FormValuesValidator.php` with a generic 422.

The shapes that validator actually enforces:

    text / textarea / date / email   bare scalar
    radio                            BARE STRING - the option's text  (:30-34)
    dropdown                         ONE object {"id": N, "text": "..."} (:22-29)
    multiselect                      LIST of those objects             (:35-53)
    table                            list with one entry per column    (:96-103)
    assignees_form                   {"users": [], "guests": [], "groups": []}
    file                             list of file objects              (:124-157)

dropdown and radio are asymmetric BY DESIGN - object versus bare text. That
asymmetry is the single most common integration mistake, and it is why this
module exists.

Two api-v2 details drive the rules below:

1.  The dropdown and multiselect arms require BOTH `id` and `text`, and require
    them to resolve to the SAME option. Text is compared with `==` against
    `array_column($capture->options, 'text', 'id')` - no trimming, no case
    folding. So "hold" does not match "HOLD".

2.  `selected: true` is NOT required at launch (`$complete` is false on that
    path), so a multiselect without it validates and stores - and then renders
    as an EMPTY STRING wherever the field is used as a `{{variable}}`, because
    `VariableReplacement.php:266-276` skips any option lacking it. Silent, and
    worse than a 422. We therefore default it to true.

Design: strict guidance in the tool description, lenient parsing here - the same
contract the CLI's `internal/cli/kickoff.go` already implements. An unresolvable
value raises a ToolError naming the valid options, which is far more actionable
than the API's "Invalid dropdown choice selected."
"""

from typing import Any, Dict, List, Optional

from fastmcp.exceptions import ToolError

# Types whose value shape depends on the field's declared options.
CHOICE_FIELD_TYPES = frozenset({"dropdown", "radio", "multiselect"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off either a dict or a dataclass/object (SDK returns both)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def option_pairs(options: Any) -> List[Dict[str, Any]]:
    """
    Normalize a field's `options` into a list of {"id", "text"} dicts.

    Only options carrying BOTH a real integer id and a text are usable, because
    those are the two halves api-v2 requires and cross-checks. Anything else is
    skipped rather than repaired: `CaptureRequestValidator.php:29-30` makes
    `options.*.id` a required integer, so a missing id means we are looking at
    something we do not understand, and inventing a positional id there would
    hand the API a confidently wrong `{"id": <guess>}`.

    If that leaves nothing, callers fall back to passing the value through
    untouched, which is exactly the behaviour that predates this module.
    """
    pairs: List[Dict[str, Any]] = []
    for opt in options or []:
        if isinstance(opt, (str, int, float, bool)):
            continue  # bare option with no id - nothing to resolve against
        text = _get(opt, "text")
        if text is None:
            text = _get(opt, "label")  # some payloads carry `label`
        if text is None:
            text = _get(opt, "value")  # ...and some carry `value`
        opt_id = _get(opt, "id")
        if text is None or opt_id is None:
            continue
        pairs.append({"id": opt_id, "text": str(text)})
    return pairs


def _ambiguous(value: Any, first: Dict[str, Any], second: Dict[str, Any],
               reason: str, label: str) -> None:
    raise ToolError(
        f'Value {value!r} is ambiguous on field "{label}": it {reason} '
        f'(id {first["id"]}={first["text"]!r} and id {second["id"]}={second["text"]!r}). '
        f'Pass the full {{"id": N, "text": "..."}} object to say which one you mean.'
    )


def _match_option(value: Any, pairs: List[Dict[str, Any]], label: str = "",
                  field_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Resolve one caller-supplied value to its canonical {"id", "text"} option.

    Accepts an option id, the option text, or an object carrying either. The
    CANONICAL text is always what gets returned, because api-v2 compares it
    byte-for-byte with no trim and no case fold.

    An OBJECT carrying both halves is resolved the way api-v2 itself does -
    look the id up, then check the given text against THAT option's text
    (`$id_text_array[$values['id']] == $values['text']`). It is deliberately
    NOT re-run through the bare-value ambiguity rules below: supplying both
    halves is precisely how a caller says which option it means, so treating
    it as ambiguous would make the documented escape hatch unusable.

    For a BARE value, precedence depends on the type, because api-v2 does:

      radio                    TEXT first. Its arm is
                               `in_array($values, $id_text_array)`, and that
                               array is `array_column(options,'text','id')` -
                               so membership is tested against the TEXTS. An
                               option id is never a valid radio value, which
                               makes the text reading the only one that can
                               round-trip.
      dropdown / multiselect   ID first, then text. Both halves are required
                               and cross-checked, so both are meaningful.

    Then EXACT text before case-folded text. That order matters: with options
    "Open" and "OPEN", api-v2 compares with `==` and no folding, so the exact
    spelling already identifies one option and folding first could rewrite a
    value that was correct.

    A bare value raises only when the RESULT would genuinely differ between
    readings - two candidates that both emit the same thing are not ambiguous.
    """
    if isinstance(value, dict):
        return _match_option_object(value, pairs, label, field_type)

    if value is None:
        return None

    as_str = str(value).strip()
    by_id = _by_id(as_str, pairs)

    exact = [p for p in pairs if p["text"].strip() == as_str]
    folded = as_str.casefold()
    fuzzy = [p for p in pairs if p["text"].strip().casefold() == folded]
    by_text = exact or fuzzy

    # radio emits the bare text, so id and text readings can only conflict when
    # they would emit DIFFERENT text. Text wins outright - see the docstring.
    if field_type == "radio":
        candidates = by_text or ([by_id] if by_id else [])
    else:
        candidates = ([by_id] if by_id else []) + by_text

    if not candidates:
        return None

    _reject_if_output_differs(value, candidates, field_type, label)
    return candidates[0]


def _by_id(as_str: str, pairs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Look up by option id, compared as strings so 2 and "2" both match."""
    return next((p for p in pairs if str(p["id"]) == as_str), None)


def _reject_if_output_differs(value: Any, candidates: List[Dict[str, Any]],
                              field_type: Optional[str], label: str) -> None:
    """
    Raise only when the candidates would produce genuinely different values.

    What counts as "different" depends on what the type emits: radio emits the
    bare text, so two options sharing a text are interchangeable, while
    dropdown and multiselect emit {"id","text"} and so a differing id matters.
    Judging ambiguity on the OUTPUT rather than on the candidate list is what
    keeps duplicate-text option sets (which exist in production) writable.
    """
    def emitted(candidate: Dict[str, Any]) -> Any:
        if field_type == "radio":
            return candidate["text"]
        return (candidate["id"], candidate["text"])

    first = candidates[0]
    second = next((c for c in candidates[1:] if emitted(c) != emitted(first)), None)
    if second is None:
        return
    _ambiguous(value, first, second, "could mean more than one option", label)


def _match_option_object(value: Dict[str, Any], pairs: List[Dict[str, Any]],
                         label: str, field_type: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve an explicit {"id", "text"} pair, mirroring api-v2's own check."""
    raw_id = value.get("id")
    text_key = next(
        (k for k in ("text", "label", "value") if value.get(k) is not None), None
    )
    raw_text = str(value[text_key]).strip() if text_key else None

    if raw_id is not None:
        by_id = _by_id(str(raw_id).strip(), pairs)
        if by_id is not None:
            if raw_text is None:
                return by_id
            canonical = by_id["text"].strip()
            if canonical == raw_text or canonical.casefold() == raw_text.casefold():
                return by_id
            # api-v2 rejects a contradictory pair outright, and the caller most
            # likely holds a stale option list. Resolving it silently would pick
            # a side and store the wrong option, so say so instead.
            raise ToolError(
                f'Contradictory option on field "{label}": id {raw_id} is '
                f'{by_id["text"]!r}, but the text given was {value[text_key]!r}. '
                f"Re-read the field's options and send one option's own id and text."
            )
        # The id resolves to nothing. Fall back to the text, which is the more
        # trustworthy half when the caller is working from a stale option list -
        # the same reasoning that makes a contradictory pair an error.
    if raw_text is not None:
        return _match_option(raw_text, pairs, label, field_type)
    return None


def _reject(value: Any, pairs: List[Dict[str, Any]], field_type: str, label: str) -> None:
    choices = ", ".join(f'{p["id"]}={p["text"]}' for p in pairs) or "(none defined)"
    raise ToolError(
        f'Value {value!r} does not match any option on {field_type} field "{label}". '
        f"Valid options: {choices}. "
        f"Pass either the option id or its exact text."
    )


def _is_clear(value: Any) -> bool:
    """
    True when the value means "no value", following PHP's `empty()`.

    `FormValuesValidator` guards every arm with `empty($values)`, so `null`,
    `false`, `0`, `""`, and `[]` are all accepted as clearing the field. Those
    have to pass straight through: coercing them would turn a legitimate clear
    into a "matches no option" error.

    ONE DELIBERATE DIVERGENCE: PHP's `empty("0")` is also true, but the string
    "0" is NOT treated as a clear here. It is a perfectly good option text, and
    live-verified as one - a kickoff radio with options "0"/"1" stored "0" and
    read it back. In api-v2 that `empty()` only short-circuits the "invalid
    choice" ERROR; the value is still stored. So the divergence only shows up
    for a "0" that matches NO option, where this raises with the option list
    while api-v2 would accept the garbage. Naming the bad value is the more
    useful of the two.
    """
    return value is None or value is False or value == 0 or not value


def coerce_field_value(
    value: Any,
    field_type: Optional[str],
    options: Any = None,
    label: str = "",
) -> Any:
    """
    Coerce one value into the shape `FormValuesValidator` requires for its type.

    Non-choice types and unknown types pass through untouched - this function
    only rewrites what it can prove it understands. Empty values pass through
    too, because clearing a field is legitimate on every type.
    """
    if field_type not in CHOICE_FIELD_TYPES:
        return value

    if _is_clear(value):
        return value

    pairs = option_pairs(options)
    if not pairs:
        # No usable options to resolve against - do not guess, leave it to the
        # API. List-backed choice fields (`$capture->list`) land here too, and
        # passthrough keeps them working exactly as they did before.
        return value

    if field_type == "dropdown":
        # A one-element list is the classic multiselect/dropdown mix-up.
        if isinstance(value, list):
            if len(value) != 1:
                raise ToolError(
                    f'dropdown field "{label}" takes a single option, not a list of '
                    f"{len(value)}. Use multiselect for multiple choices."
                )
            value = value[0]
        match = _match_option(value, pairs, label, field_type)
        if match is None:
            _reject(value, pairs, field_type, label)
        return {"id": match["id"], "text": match["text"]}

    if field_type == "radio":
        if isinstance(value, list):
            if len(value) != 1:
                raise ToolError(
                    f'radio field "{label}" takes a single option, not a list of '
                    f"{len(value)}."
                )
            value = value[0]
        match = _match_option(value, pairs, label, field_type)
        if match is None:
            _reject(value, pairs, field_type, label)
        # Asymmetric on purpose: radio wants the BARE TEXT, not an object.
        #
        # An option whose text is literally "0" is NOT a problem, though PHP's
        # empty("0") makes it look like one. In the radio arm the guard reads
        # `if (! in_array($values, $id_text_array) && ! empty($values))`, so
        # empty() only suppresses the "invalid choice" ERROR - it never clears
        # anything, and the renderer takes radio's field_value verbatim with no
        # empty() guard of its own. Live-verified on a kickoff radio whose
        # options were "0" and "1": launching with "0" returned 201 and read
        # back as {"<field_id>": "0"}. Refusing to send it would break a case
        # the API handles correctly.
        return match["text"]

    # multiselect
    items = value if isinstance(value, list) else [value]
    coerced: List[Dict[str, Any]] = []
    for item in items:
        if item is None:
            continue
        match = _match_option(item, pairs, label, field_type)
        if match is None:
            _reject(item, pairs, field_type, label)
        # Respect an explicit selected=False; default the missing case to True,
        # since omitting it renders the field empty in every variable position.
        selected = True
        if isinstance(item, dict) and item.get("selected") is False:
            selected = False
        coerced.append(
            {"id": match["id"], "text": match["text"], "selected": selected}
        )
    return coerced


def coerce_field_values_safely(
    payload: Optional[Dict[str, Any]],
    load_fields: Any,
) -> Optional[Dict[str, Any]]:
    """
    Coerce, but never let the field-definitions READ block a write.

    Coercion needs the field definitions, which means an extra GET on a path
    that previously did none. That GET must not be able to fail a call that
    used to succeed: on any read error we send the payload uncoerced, exactly
    as before this module existed, and let the API stay the authority.

    Only the READ is guarded. A ToolError from `coerce_field_values` itself is
    a real "this value matches no option" verdict and propagates - swallowing
    it would put us back to a generic 422 with the option list thrown away.

    `load_fields` is a zero-arg callable so nothing is fetched when `payload`
    is empty, which is the common case.

    Both write surfaces share this one helper on purpose. The same bug was
    reported on `launch_process` while `_coerce_taskdata` had the guard, which
    is the sibling-drift failure CLAUDE.md rule 16 is about.
    """
    if not payload:
        return payload
    try:
        fields = load_fields()
    except Exception:
        return payload
    return coerce_field_values(payload, fields)


def coerce_field_values(
    payload: Optional[Dict[str, Any]],
    fields: Any,
) -> Optional[Dict[str, Any]]:
    """
    Coerce every value in an ID-keyed payload using its field definition.

    `fields` is the list of field definitions for the surface being written -
    `template.prerun` for a launch, a step's form fields for `taskdata`. Keys
    with no matching definition pass through untouched: an unknown key is the
    caller's problem to see, and silently rewriting it would hide it.

    Matching is by the field's `id`, which for kickoff fields IS the
    `timeline_id` (`PrerunTransformer.php:12` maps `'id' => $prerun->timeline_id`).
    """
    if not payload:
        return payload

    by_id: Dict[str, Any] = {}
    for field in fields or []:
        field_id = _get(field, "id")
        if field_id is not None:
            by_id[str(field_id)] = field

    if not by_id:
        return payload

    out: Dict[str, Any] = {}
    for key, value in payload.items():
        field = by_id.get(str(key))
        if field is None:
            out[key] = value
            continue
        out[key] = coerce_field_value(
            value,
            _get(field, "field_type"),
            _get(field, "options"),
            label=_get(field, "label") or _get(field, "alias") or str(key),
        )
    return out
