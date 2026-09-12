"""Guest email validation and launch-time guest assignment (#1290).

WHY THIS MODULE EXISTS, AND WHY ``guests`` IS NOT A TOP-LEVEL LAUNCH KEY
-----------------------------------------------------------------------

``POST /organizations/{org}/runs`` does NOT accept a top-level ``guests`` key,
and a payload carrying one is discarded in silence. Measured on api-v2
``origin/master`` 2026-09-11, three independent reasons, any one of which is
enough:

1. ``RunService::startRun`` (``app/Services/RunService.php:42``) builds the run's
   assignees with ``Assignees::newFromArray(Arr::only($data, ['users', 'groups']))``.
   ``Arr::only`` strips ``guests`` before ``BaseAssignees::newFromArray`` ever
   sees it, even though that constructor reads ``$data['guests'] ?? []`` and
   would have honoured it.
2. ``Run`` declares ``users()`` and ``groups()`` relations and NO ``guests()``
   relation (``app/Models/Run.php:1752``, ``:1757``), so
   ``AssignableTrait::saveAssignees`` skips the guest branch entirely: it is
   guarded by ``hasGuestsRelation()``, which tests
   ``method_exists($this, 'guests')``.
3. There is no ``runs_guests`` table. The schema carries ``steps_guests``,
   ``tasks_guests`` and ``organizations_guests`` and nothing at run level.

``CreateRunRequest::rules()`` also declares no top-level ``guests`` rule, though
that one on its own would not have mattered: ``RunsControllerNew::store`` passes
``$request->all()`` rather than ``onlyValidatedFields()``, so an unruled key
survives validation here and dies at ``Arr::only`` instead.

So a guest cannot be a member of a process. What a guest CAN be is an assignee
of that process's TASKS, and api-v2 accepts exactly one shape for that at launch
time::

    tasks: {"<step_timeline_id>": {"owners": {"guests": ["a@b.com"]}}}

validated by ``CreateRunRequest`` as ``tasks.*.owners.guests => ['array', new
ValidGuestEmail]`` and consumed by ``TaskFactory::buildFromStep``, which reads
``Assignees::newFromArray($input['owners'] ?? [])``.

TWO api-v2 BEHAVIOURS MAKE THE NAIVE PAYLOAD DESTRUCTIVE
--------------------------------------------------------

**Supplying ``owners`` REPLACES the step's own assignees.**
``TaskFactory::buildFromStep`` falls back to ``$step->wrappedAssignees()`` only
when the supplied bucket is empty, so naming a step and handing it just the
guest drops whoever the template assigned. Every entry this module builds
therefore re-sends the step's own ``assignees`` / ``guests`` / ``groups`` and
adds to them, which is the same read-modify-write rule the repo already applies
to the step endpoint.

**A PARTIAL ``tasks`` map TRUNCATES THE PROCESS.**
``AddTasks::getSteps`` (``app/Tallyfy/Domain/Observers/Checklists/Run/Created/
AddTasks.php``) creates tasks only for the steps named in ``tasks`` whenever
that key is non-empty, and its ``if (empty($steps))`` fallback cannot rescue it,
because ``$steps`` is an Eloquent Collection by then and ``empty()`` is false for
any object. So a launch naming three of twelve steps produces a three-task
process, with a 201 and nothing said. Every map this module builds therefore
covers EVERY step of the template.

``allow_guest_owners`` is the template author's per-step declaration of whether
that step may go to a guest. It is ``DEFAULT true NOT NULL`` in the schema, so
the common case is permissive, and honouring it means a step somebody
deliberately closed to guests stays closed. Nothing in api-v2 enforces it on
this path, which is exactly why it is enforced here.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from fastmcp.exceptions import ToolError

# Deliberately permissive, and deliberately not RFC 5322. api-v2's
# ``ValidGuestEmail`` is the authority: it runs Laravel's ``rfc`` and ``dns``
# validations, rejects banned hosts, rejects a bot address, rejects an address
# that is already a MEMBER of this organization, and rejects an SSO company
# domain. Re-implementing any of that here would diverge the moment api-v2
# changes it, and would refuse addresses the API accepts. This pattern exists
# only to catch the shapes that are obviously not an email at all -- a bare
# name, a user id, a blank -- so the caller gets a message naming the offending
# value instead of a 422 that names the whole array.
_LOOKS_LIKE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Buckets ``Assignees::newFromArray`` reads, in the order api-v2 declares them.
_OWNER_BUCKETS = ("users", "guests", "groups")

#: api-v2 ``User::BOT`` (``app/Models/User.php``). A bot caller is the one case
#: where the run starter is NOT the authenticated user.
_BOT_USER_TYPE = "bot"


def normalize_guest_emails(values: Any, parameter: str = "guests") -> List[str]:
    """Validate and de-duplicate a list of guest email addresses.

    Accepts a bare string for the single-guest case, because every other
    list-shaped parameter in this server does.

    Raises
    ------
    ToolError
        If any entry is not a string, is blank, or does not look like an email
        address. The message names the offending value, because an error that
        only says "invalid email" makes the caller re-send the whole list.
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ToolError(
            f"'{parameter}' must be a list of guest email addresses, e.g. "
            f"{parameter}=[\"person@example.com\"]. Got {type(values).__name__}."
        )

    seen: Dict[str, str] = {}
    for entry in values:
        if not isinstance(entry, str) or not entry.strip():
            raise ToolError(
                f"'{parameter}' entries must be non-empty email addresses. "
                f"Got {entry!r}. Guests are identified by EMAIL, never by a "
                f"numeric member id -- member ids go in 'users'."
            )
        email = entry.strip()
        if not _LOOKS_LIKE_EMAIL.match(email):
            raise ToolError(
                f"'{email}' is not an email address, so it cannot identify a "
                f"guest. Guests are identified by email; organization members "
                f"are identified by numeric id and belong in 'users'."
            )
        seen.setdefault(email.lower(), email)

    return list(seen.values())


def _step_owner_buckets(step: Any) -> Dict[str, List[Any]]:
    """Read a step's CURRENT assignees, or refuse.

    ``StepTransformer`` emits ``assignees``, ``guests`` and ``groups``
    unconditionally, so a missing or non-list bucket means the READ failed
    rather than that the step is unassigned. Sending ``[]`` on a failed read
    would detach whoever is on the step, which is the wipe this whole module is
    written to avoid, so an unreadable bucket aborts instead.
    """
    # The SDK's Step dataclass spells the member bucket ``assignees`` while the
    # launch payload spells it ``users``. ``BaseAssignees::newFromArray`` reads
    # ``$data['users'] ?? $data['assignees'] ?? []``, so either key works on the
    # wire; ``users`` is written here so the payload matches what the rest of
    # this tool sends.
    raw = {
        "users": getattr(step, "assignees", None),
        "guests": getattr(step, "guests", None),
        "groups": getattr(step, "groups", None),
    }
    buckets: Dict[str, List[Any]] = {}
    for bucket, value in raw.items():
        if value is None:
            # A genuinely absent bucket on the SDK model is treated as empty
            # rather than as a failed read: unlike the api-v2 transformer, the
            # dataclass defaults these to None when the key was absent AND when
            # it was an empty list, and the two are indistinguishable here.
            buckets[bucket] = []
            continue
        if not isinstance(value, (list, tuple)):
            raise ToolError(
                f"Step '{getattr(step, 'id', '?')}' returned a "
                f"'{bucket}' bucket that is not a list ({type(value).__name__}). "
                "Refusing to launch: re-sending an unreadable bucket would "
                "detach whoever is currently assigned to that step."
            )
        buckets[bucket] = list(value)
    return buckets


def _merge_bucket(existing: Sequence[Any], additions: Iterable[Any]) -> List[Any]:
    """Union two assignee buckets, preserving order and first-seen spelling."""
    merged: List[Any] = list(existing)
    lowered = {v.lower() for v in merged if isinstance(v, str)}
    for value in additions:
        key = value.lower() if isinstance(value, str) else value
        if isinstance(key, str):
            if key in lowered:
                continue
            lowered.add(key)
        elif value in merged:
            continue
        merged.append(value)
    return merged


def resolve_run_starter_user_id(sdk: Any, org_id: str, template_id: str) -> Optional[int]:
    """Who api-v2 would call the run starter for a launch made with this token.

    Replicates ``TaskFactory::buildFromStep`` (api-v2
    ``app/Domain/Task/TaskFactory.php``) rather than approximating it::

        $newAssignees->addExtra([
            ! auth_user()->isBot() ? auth_user()->id : $process->checklist->owner_id
        ]);

    So it is the authenticated user, except for a bot token, where api-v2 uses
    the TEMPLATE's owner instead. ``User::isBot()`` is ``type === 'bot'``
    (api-v2 ``app/Models/User.php:549``, with ``const BOT = 'bot'``), and
    ``UserTransformer`` emits ``type`` on the wire, so the value is readable.

    The SDK's ``User`` dataclass has no ``type`` field today, so the value
    arrives in the lossless ``extra`` passthrough. Both spellings are read, in
    that order, so promoting ``type`` to a real field later does not silently
    break this: the known-key set is derived from ``dataclasses.fields()``, so
    a promoted key LEAVES ``extra`` and an ``extra``-only read stops finding it
    with no error. That is the same trap ``assign_run_starter`` just walked
    into, one field over.

    Returns ``None`` when the answer cannot be established, which the caller
    turns into a refusal. Guessing here would put the wrong person on a task.
    """
    me = sdk.users.get_current_user_info(org_id)
    if me is None:
        return None

    user_type = getattr(me, "type", None)
    if user_type is None:
        user_type = (getattr(me, "extra", None) or {}).get("type")

    if user_type == _BOT_USER_TYPE:
        template = sdk.templates.get_template(org_id, template_id=template_id)
        return getattr(template, "owner_id", None) if template is not None else None

    return getattr(me, "id", None)


def _would_assign_the_run_starter(step: Any, step_id: str, buckets: Dict[str, List[Any]]) -> bool:
    """Is this a step whose task api-v2 would hand to whoever launched the run?

    Only true when BOTH halves hold, because api-v2 checks them in that order
    (``TaskFactory::buildFromStep``, api-v2 ``app/Domain/Task/TaskFactory.php``):

        $newAssignees = Assignees::newFromArray($input['owners'] ?? []);
        if ($newAssignees->isEmpty()) { $newAssignees = $step->wrappedAssignees(); }
        if ($newAssignees->isEmpty() && $step->assign_run_starter) { ... }

    So a step with its own assignees never reaches the run-starter branch, and
    neither does one where the caller supplied owners. ``buckets`` here already
    holds both of those, which is why it is the thing tested rather than the
    step's raw fields.

    ``BaseAssignees::isNotEmpty`` (api-v2 ``app/Domain/Owners/BaseAssignees.php``)
    counts a bucket non-empty if users OR guests OR groups is truthy, so an
    empty list in every bucket is what "empty" means on the wire.
    """
    if any(buckets[bucket] for bucket in _OWNER_BUCKETS):
        return False

    flag = getattr(step, "assign_run_starter", None)
    if flag is None:
        # Refusing rather than guessing, and the direction matters. Guessing
        # False re-creates the exact defect this function exists to close;
        # guessing True assigns somebody to a step that never asked for them.
        # api-v2 emits the key on every step (``StepTransformer::transform``),
        # and server/requirements.txt pins an SDK that carries the field, so
        # None means something upstream changed and a human should look.
        raise ToolError(
            f"Step '{step_id}' came back with no 'assign_run_starter' value, so "
            "this tool cannot tell whether adding a guest would drop the person "
            "who launched the process. Refusing to launch. api-v2 sends this "
            "field on every step and the pinned SDK exposes it, so this means "
            "the API or the SDK pin changed. Assign the step explicitly with "
            "'tasks' to launch in the meantime."
        )
    return bool(flag)


def build_guest_task_overrides(
    steps: Sequence[Any],
    guest_emails: Sequence[str],
    existing_tasks: Any = None,
    run_starter_resolver: Optional[Callable[[], Optional[int]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build the ``tasks`` payload that assigns ``guest_emails`` at launch.

    Every step of the template gets an entry, because a partial ``tasks`` map
    makes api-v2 create tasks for the named steps only. Each entry re-sends that
    step's own assignees, because supplying ``owners`` replaces them. The guest
    emails are added only to steps whose ``allow_guest_owners`` is true.

    Anything the caller already supplied in ``existing_tasks`` is preserved
    verbatim apart from its ``owners`` bucket, which is merged rather than
    overwritten.

    A step that assigns whoever launched the process, and has no assignees of
    its own, gets that person named explicitly. api-v2 only reaches its
    run-starter fallback when the ``owners`` it is handed is empty, so adding a
    guest to such a step would otherwise leave the guest as the sole owner and
    drop the launcher in silence. ``run_starter_resolver`` supplies the id and
    is called at most once, only when a step actually needs it.

    Raises
    ------
    ToolError
        If the template has no steps, or if no step accepts guest owners. Both
        are refusals rather than silent no-ops: a 201 describing a process the
        guest was never put on is the failure this parameter exists to remove.

        Also if a step would have assigned the run starter and this tool cannot
        say who that is, either because no resolver was supplied or because the
        step carries no ``assign_run_starter`` value to read. Both are refusals
        for the same reason: launching anyway produces a 201 and a task the
        launcher was quietly removed from.
    """
    if not guest_emails:
        raise ToolError("build_guest_task_overrides called with no guest emails.")
    if not steps:
        raise ToolError(
            "This template has no steps, so there is no task a guest could be "
            "assigned to. A guest cannot be a member of a process itself: "
            "Tallyfy has no such relationship. Add a step, or collect the guest "
            "through a kickoff 'assignees_form' field via prerun."
        )

    supplied = existing_tasks if isinstance(existing_tasks, dict) else {}
    overrides: Dict[str, Dict[str, Any]] = {}
    accepted: List[str] = []
    kept_starter: List[str] = []

    # Memoised, and deliberately lazy: most templates have no step that needs
    # this, and resolving costs a round trip. `_memo` holds at most one element
    # so a resolver returning None is still only called once.
    _memo: List[Optional[int]] = []

    def _run_starter_id() -> Optional[int]:
        if not _memo:
            _memo.append(run_starter_resolver() if run_starter_resolver else None)
        return _memo[0]

    for step in steps:
        step_id = getattr(step, "id", None)
        if not step_id:
            raise ToolError(
                "A step came back with no id, so the launch payload cannot name "
                "every step. Refusing: a tasks payload that names only some "
                "steps makes api-v2 create tasks for those steps ALONE."
            )
        step_id = str(step_id)

        entry: Dict[str, Any] = dict(supplied.get(step_id) or {})
        buckets = _step_owner_buckets(step)

        caller_owners = entry.get("owners")
        if isinstance(caller_owners, dict):
            for bucket in _OWNER_BUCKETS:
                caller_bucket = caller_owners.get(bucket)
                if isinstance(caller_bucket, (list, tuple)):
                    buckets[bucket] = _merge_bucket(buckets[bucket], caller_bucket)

        if getattr(step, "allow_guest_owners", True):
            # ORDER IS LOAD-BEARING. This has to be asked BEFORE the guest goes
            # in, because adding the guest is the thing that makes the bucket
            # non-empty and hides the case from api-v2 as well as from here.
            if _would_assign_the_run_starter(step, step_id, buckets):
                starter_id = _run_starter_id()
                if starter_id is None:
                    raise ToolError(
                        f"Step '{step_id}' assigns whoever launches the process "
                        "and has no assignees of its own, so adding a guest "
                        "would leave the guest as the only owner and silently "
                        "drop the launcher. This tool could not work out who "
                        "the launcher is, so it is refusing instead of guessing. "
                        "Name the owners for that step explicitly with 'tasks'."
                    )
                buckets["users"] = _merge_bucket(buckets["users"], [starter_id])
                kept_starter.append(step_id)

            buckets["guests"] = _merge_bucket(buckets["guests"], guest_emails)
            accepted.append(step_id)

        entry["owners"] = buckets
        overrides[step_id] = entry

    if not accepted:
        raise ToolError(
            "Every step in this template has allow_guest_owners disabled, so no "
            "task can be assigned to a guest. Enable guest owners on the steps "
            "the guest should work on, or assign the guest through a kickoff "
            "'assignees_form' field via prerun, or through 'roles'."
        )

    # A step the caller named that is not in the template is a caller error
    # api-v2 reports as "The task <id> is not part of the template", so it is
    # passed through untouched rather than dropped: dropping it would turn a
    # legible 422 into a silent no-op.
    for step_id, entry in supplied.items():
        overrides.setdefault(str(step_id), entry)

    return overrides
