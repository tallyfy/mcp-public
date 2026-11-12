"""Scheduled jobs (api-v2 ``recurring-jobs``): building a schedule, and the two readings.

The scheduled launch tools in ``tools/process_management.py`` (tallyfy/mcp#1603) take
a day and a time in the organization's LOCAL time and turn them into the body
api-v2 stores. Everything here is pure, so the decisions can be tested without a
network. The tools call api-v2 through SDK 4.0's ``sdk.recurring_jobs``
(tallyfy/sdk#199), and the jobs read here are its typed ``RecurringJob`` objects.

Two api-v2 contracts are live at once, and they read the same body differently:

===============================  =======================================  ==========================================
                                 api-v2 ``production`` (``0e89c2406``)     api-v2 ``master``, staging (api-v2#8974)
===============================  =======================================  ==========================================
``start_date``, ``start_time``   read in UTC                              read in the organization's time zone
weekly days                      ``schedule.day_of_week``, one integer    ``schedule.days_of_week``, a list
``next_occurrence`` on a job     never returned                           returned (null unless active)
===============================  =======================================  ==========================================

Read from ``RecurringJobService::build()``, ``CreateRecurringJobRequest``,
``RecurringSchedule::$fields`` and ``RecurringJobTransformer`` on both branches on
2026-10-14. The tools never decide which one they are talking to from
configuration, because the MCP server and api-v2 promote separately. They read
it off the API itself: ``reading_from_job`` from any job (the SDK's
``RecurringJob.next_occurrence_sent``), and the SDK's ``supports_days_of_week``
from a create that can never be saved, for an organization with no job to read.
Both arms were measured on 2026-10-14, on staging and on the production test org,
and the probe created nothing on either.

A job is CREATED only on the organization reading (owner decision 2026-10-15 on
tallyfy/mcp#1604). Two reviews found that a schedule converted to UTC cannot keep
its local day on production's roll in several cases, and that every job saved
that way would move when api-v2#8974 reaches production. So on the UTC reading
``schedule_process_launch`` refuses with ``UTC_CREATE_REFUSAL`` and nothing here
converts a time. Listing, reading, pausing, resuming and deleting work on both
readings, and pause and resume send the stored times back unchanged (the SDK's
``set_recurring_job_status`` re-sends the job as read). Creation on production
comes back once #8974 is promoted there (tallyfy/work-queue#4020).

A job runs as its owner, kept in ``data.created_by``. api-v2#11639 adds
``PUT recurring-jobs/{id}/owner`` (admins only) and a top-level ``owner`` object,
``{id, full_name, email}``, on every job it returns (tallyfy/mcp#1720). An older
api-v2 sends only ``data.created_by``, so ``job_owner`` reads the object when it
is there and the id otherwise, and ``owner_route_answer`` tells a 404 for the
missing route from a 404 for a missing job by the ``code`` in api-v2's body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as _dt_timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from tallyfy.models import RecurringJob, RecurringJobHistory, RecurringSchedule
from tallyfy.recurring_job_management import (
    RESOURCE_NOT_FOUND_CODE,
    ROUTE_NOT_FOUND_CODE,
)

from utils.date_utils import resolve_zone

LAUNCH_JOB_KEY = "launch-process"

# Which clock api-v2 reads a schedule's start_date and start_time on.
READING_ORGANIZATION = "organization"
READING_UTC = "utc"

REPEATS = ("once", "hourly", "daily", "weekly", "monthly", "yearly")

# CreateRecurringJobRequest / UpdateRecurringJobRequest: 'name' => 'required|string|max:50',
# 'description' => 'string|max:500'. The launch-process type's own validations
# (recurring_job_types row, read from GET recurring-job-types on staging and
# production 2026-10-14): 'name' => 'string|max:300'.
JOB_NAME_MAX = 50
DESCRIPTION_MAX = 500
PROCESS_NAME_MAX = 300

# RecurringSchedule::$fields on either branch: the schedule keys a job is shown with.
SCHEDULE_FIELDS = (
    "recurring_type",
    "interval",
    "adjust_to_nearest_date",
    "start_time",
    "day_of_week",
    "days_of_week",
    "week_of_month",
    "day_of_month",
    "month_of_year",
)

FINISHED_MESSAGE = (
    "This job has finished, so it cannot be paused or resumed. Schedule it again "
    "as a new job with schedule_process_launch."
)

# What schedule_process_launch answers on the UTC reading, before any create.
UTC_CREATE_REFUSAL = (
    "Scheduled launches through the assistant are not available on this Tallyfy "
    "server yet, so nothing was scheduled. Listing, reading, pausing, resuming and "
    "deleting scheduled jobs still work."
)

# change_scheduled_job_owner (tallyfy/mcp#1720). The route and the 403 are
# api-v2#11639's contract: only an organization admin may change an owner.
OWNER_ADMIN_ONLY = (
    "Only an organization admin can change a scheduled job's owner, so nothing was changed."
)
OWNER_ROUTE_MISSING = (
    "This Tallyfy server does not support changing a scheduled job's owner yet, so "
    "nothing was changed."
)
OWNER_KEYS = ("id", "full_name", "email")

# The `code` api-v2's Handler::render puts in a 404 body (app/Exceptions/Handler.php
# on production and master, read 2026-11-03) is the SDK's ROUTE_NOT_FOUND_CODE for a
# route that does not exist (NotFoundHttpException) and RESOURCE_NOT_FOUND_CODE for a
# job that does not exist or that the caller may not see (ModelNotFoundException,
# RecurringJobPolicy::authorizeRead). Measured on staging the same day: PUT
# recurring-jobs/999999999/owner answered the first, GET recurring-jobs/999999999
# the second.
OWNER_404_ROUTE = "route"
OWNER_404_JOB = "job"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_API_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")


class ScheduleInputError(ValueError):
    """The schedule asked for cannot be built. The message is for the caller."""


@dataclass(frozen=True)
class PlannedSchedule:
    """What to send for one schedule, and what it means in local time."""

    start_date: str
    schedule: Dict[str, Any]
    max_occurrences: Optional[int]
    end_date: Optional[str]
    first_run_local: datetime
    zone_name: str
    notes: List[str] = field(default_factory=list)


def parse_day(value: Any, what: str) -> date:
    """A calendar day written ``YYYY-MM-DD``."""
    text = value.strip() if isinstance(value, str) else ""
    if not _DAY_RE.match(text):
        raise ScheduleInputError(f"{what} must be a day written YYYY-MM-DD, such as 2027-03-01. Got {value!r}.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ScheduleInputError(f"{what} {value!r} is not a real date.") from None


def parse_clock(value: Any, what: str = "time") -> time:
    """A 24 hour time written ``HH:MM`` (``H:MM`` is accepted)."""
    text = value.strip() if isinstance(value, str) else ""
    match = _CLOCK_RE.match(text)
    if not match:
        raise ScheduleInputError(f"{what} must be a 24 hour time written HH:MM, such as 09:00 or 17:30. Got {value!r}.")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ScheduleInputError(f"{what} {value!r} is not a time of day.")
    return time(hours, minutes)


def api_clock(value: Any) -> Optional[str]:
    """A stored start_time (``09:00:00``) as the ``HH:MM`` api-v2 takes back.

    The column is a Postgres ``time`` and comes back with seconds, which
    ``date_format:H:i`` refuses (measured on staging 2026-10-14).
    """
    if not isinstance(value, str):
        return None
    match = _API_TIME_RE.match(value.strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def reading_from_job(job: RecurringJob) -> str:
    """Which clock the API that returned ``job`` reads schedules on.

    Only an api-v2 with #8974 writes ``next_occurrence``, and it writes the key on
    every job, null unless the job is active. The SDK keeps whether the key was
    sent, whatever its value, as ``next_occurrence_sent``.
    """
    return READING_ORGANIZATION if job.next_occurrence_sent else READING_UTC


def _normalize_days(days_of_week: Optional[Iterable[Any]]) -> Optional[List[int]]:
    if days_of_week is None:
        return None
    days: List[int] = []
    for value in days_of_week:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 7:
            raise ScheduleInputError(
                f"days_of_week takes whole numbers from 1 (Monday) to 7 (Sunday). Got {value!r}."
            )
        if value not in days:
            days.append(value)
    if not days:
        raise ScheduleInputError("days_of_week is empty. Leave it out to run on the start date's weekday.")
    return sorted(days)


def _first_weekly_day_on_master(start: date, days: Sequence[int], interval: int) -> date:
    """The first run master's ``weekly()`` picks.

    For each day it starts from that weekday in start's week (Monday first) and
    adds ``interval`` weeks until it is not before start, then takes the
    earliest. So with an interval of 2, a day earlier in the week than start
    runs first two weeks on, not next week.
    """
    monday = start - timedelta(days=start.isoweekday() - 1)
    firsts = []
    for weekday in days:
        candidate = monday + timedelta(days=weekday - 1)
        while candidate < start:
            candidate += timedelta(weeks=interval)
        firsts.append(candidate)
    return min(firsts)


def plan_schedule(
    *,
    repeat: str,
    start_date: Any,
    time_of_day: Any,
    interval: Optional[int],
    days_of_week: Optional[Sequence[Any]],
    end_date: Optional[str],
    max_occurrences: Optional[int],
    tz_name: Optional[str],
    now: Optional[datetime] = None,
) -> PlannedSchedule:
    """Turn a local day and time into the start_date and schedule api-v2 stores.

    For the organization reading only: the local values are sent as they are,
    because api-v2 reads them in the organization's zone. ``start_date`` is the
    day of the first run. A weekly job runs first on the day master's
    ``weekly()`` picks, a monthly job on its day of the month, a yearly job on
    its month and day.

    With ``now`` (an aware instant), a first run that is not after it is refused,
    because api-v2 would roll it forward and the reply would name a time that has
    passed. Without it that check is skipped, which is how the tool checks the
    input before it knows the organization's zone.
    """
    if repeat not in REPEATS:
        raise ScheduleInputError(f"repeat must be one of {', '.join(REPEATS)}. Got {repeat!r}.")

    day = parse_day(start_date, "start_date")
    clock = parse_clock(time_of_day, "time")

    if interval is None:
        interval = 1
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ScheduleInputError(f"interval must be a whole number of 1 or more. Got {interval!r}.")

    days = _normalize_days(days_of_week)
    if days is not None and repeat != "weekly":
        raise ScheduleInputError("days_of_week applies only when repeat is weekly.")

    if repeat == "once":
        if interval != 1:
            raise ScheduleInputError("interval does not apply to a one-time job. Leave it out.")
        if end_date is not None or max_occurrences is not None:
            raise ScheduleInputError(
                "end_date and max_occurrences do not apply to a one-time job, which runs once."
            )

    last_day = parse_day(end_date, "end_date") if end_date is not None else None
    if last_day is not None and last_day < day:
        raise ScheduleInputError("end_date cannot be before start_date.")
    if max_occurrences is not None and (
        isinstance(max_occurrences, bool) or not isinstance(max_occurrences, int) or max_occurrences < 1
    ):
        raise ScheduleInputError(f"max_occurrences must be a whole number of 1 or more. Got {max_occurrences!r}.")

    notes: List[str] = []
    zone: Optional[tzinfo] = resolve_zone(tz_name) if tz_name else None
    zone_known = True
    if tz_name and zone is None:
        # api-v2 reads it with PHP, which may know the name; only the local
        # rendering in this reply, and the check that the first run is ahead, are lost.
        zone_name = tz_name
        zone = _dt_timezone.utc
        zone_known = False
        notes.append(f"Times are read in {tz_name}, which this server cannot convert, so no local times are shown.")
    elif zone is None:
        zone_name = "UTC"
        zone = _dt_timezone.utc
        notes.append("The organization has no time zone set, so times are UTC.")
    else:
        zone_name = tz_name or "UTC"

    week_days = days or [day.isoweekday()]
    first_day = _first_weekly_day_on_master(day, week_days, interval) if repeat == "weekly" else day
    first_run_local = datetime.combine(first_day, clock, tzinfo=zone)
    if last_day is not None and last_day < first_day:
        raise ScheduleInputError(
            f"end_date {last_day.isoformat()} is before the first run on {first_day.isoformat()}, "
            "so the job would never run."
        )
    if now is not None and zone_known and first_run_local <= now:
        local_now = now.astimezone(zone)
        raise ScheduleInputError(
            f"The first run, {first_run_local.strftime('%Y-%m-%d %H:%M')} {zone_name}, has already "
            f"passed: it is {local_now.strftime('%Y-%m-%d %H:%M')} there now. Pick a later "
            "start_date or time."
        )

    schedule: Dict[str, Any] = {"start_time": f"{clock.hour:02d}:{clock.minute:02d}"}
    if repeat == "once":
        # The next-gen client's convention for a one-time job.
        schedule.update(recurring_type="daily", interval=1)
        max_occurrences = 1
    else:
        schedule.update(recurring_type=repeat, interval=interval)

    if repeat == "weekly":
        schedule["days_of_week"] = week_days
    elif repeat == "monthly":
        schedule["day_of_month"] = day.day
        if day.day > 28:
            # Without it api-v2 skips every month that lacks the day.
            schedule["adjust_to_nearest_date"] = True
            notes.append(f"In a month with fewer than {day.day} days it runs on the last day.")
    elif repeat == "yearly":
        schedule["month_of_year"] = day.month
        schedule["day_of_month"] = day.day

    return PlannedSchedule(
        start_date=day.isoformat(),
        schedule=schedule,
        max_occurrences=max_occurrences,
        end_date=last_day.isoformat() if last_day is not None else None,
        first_run_local=first_run_local,
        zone_name=zone_name,
        notes=notes,
    )


def clean_schedule(schedule: Optional[RecurringSchedule]) -> Dict[str, Any]:
    """A stored schedule as the tools show it: known keys, no nulls, HH:MM."""
    cleaned: Dict[str, Any] = {}
    if schedule is None:
        return cleaned
    for key in SCHEDULE_FIELDS:
        value = getattr(schedule, key)
        if value is None:
            continue
        if key == "start_time":
            value = api_clock(value)
            if value is None:
                continue
        cleaned[key] = value
    return cleaned


def zone_label(reading: str, tz_name: Optional[str]) -> str:
    """Which zone a job's start_date and start_time are read in, said plainly."""
    if reading == READING_UTC:
        if tz_name:
            return (
                "UTC. This Tallyfy server reads schedule times in UTC; the organization's "
                f"time zone is {tz_name}."
            )
        return "UTC."
    if tz_name:
        return f"{tz_name}, the organization's time zone."
    return "UTC, because the organization has no time zone set."


def _parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt_timezone.utc)
    return parsed


def local_string(instant: Any, tz_name: Optional[str]) -> Optional[str]:
    """An API instant (``2027-03-01T15:00:00.000000Z``) in the organization's zone."""
    parsed = _parse_instant(instant)
    if parsed is None:
        return None
    zone = resolve_zone(tz_name) if tz_name else _dt_timezone.utc
    if zone is None:
        return None
    try:
        return parsed.astimezone(zone).strftime(f"%Y-%m-%d %H:%M ({tz_name or 'UTC'})")
    except OverflowError:
        return None


def same_minute(instant: Any, expected: datetime) -> Optional[bool]:
    """Whether an API instant names the same minute as ``expected``. None if unreadable."""
    parsed = _parse_instant(instant)
    if parsed is None:
        return None
    return parsed.astimezone(_dt_timezone.utc).replace(second=0, microsecond=0) == (
        expected.astimezone(_dt_timezone.utc).replace(second=0, microsecond=0)
    )


def _user_id(value: Any) -> Optional[int]:
    """A user id as an int, from an int or a string of digits. None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    text = value.strip() if isinstance(value, str) else ""
    # isascii first: str.isdigit() is also true for a superscript two, which int() refuses.
    if text.isascii() and text.isdigit():
        number = int(text)
        return number if number >= 1 else None
    return None


def job_owner(job: RecurringJob) -> Optional[Dict[str, Any]]:
    """Who a job runs as: api-v2#11639's ``owner`` object, else ``data.created_by``.

    The object is shown as ``id``, ``full_name`` and ``email``. Without it only the
    id is known, so only the id is returned. None when the job names neither.
    """
    owner = job.owner
    if owner is not None and owner.id is not None:
        return {key: getattr(owner, key) for key in OWNER_KEYS}
    data = job.data
    created_by = data.get("created_by") if isinstance(data, dict) else None
    if created_by is not None:
        return {"id": created_by}
    return None


def same_user(left: Any, right: Any) -> bool:
    """Whether two user ids name the same person, whichever of int or string each is."""
    a, b = _user_id(left), _user_id(right)
    return a is not None and a == b


def owner_label(owner: Optional[Dict[str, Any]]) -> str:
    """An owner in words: ``Jane Doe (jane@example.com)``, or ``user 27425``."""
    if not owner:
        return "an unknown person"
    name = owner.get("full_name")
    email = owner.get("email")
    if name and email:
        return f"{name} ({email})"
    if name or email:
        return str(name or email)
    return f"user {owner.get('id')}"


def parse_new_owner(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """``new_owner`` as ``(user_id, None)`` or ``(None, email)``.

    A whole number, or a string of digits, is a user id. A string with one ``@``
    and a dot after it is an email. Anything else is refused before any call.
    """
    user_id = _user_id(value)
    if user_id is not None:
        return user_id, None
    text = value.strip() if isinstance(value, str) else ""
    if _EMAIL_RE.match(text):
        return None, text
    raise ScheduleInputError(
        "new_owner takes the new owner's email or numeric user id, such as "
        f"jane@example.com or 27425. Got {value!r}. get_organization_users lists "
        "members with both."
    )


def owner_route_answer(response_data: Any) -> Optional[str]:
    """Which 404 the owner route got, from api-v2's body.

    ``OWNER_404_ROUTE`` when the API has no such route, ``OWNER_404_JOB`` when the
    job is missing or hidden from the caller, None when the body says neither,
    so a caller can pass Tallyfy's own answer on rather than guess.
    """
    code = response_data.get("code") if isinstance(response_data, dict) else None
    if code == ROUTE_NOT_FOUND_CODE:
        return OWNER_404_ROUTE
    if code == RESOURCE_NOT_FOUND_CODE:
        return OWNER_404_JOB
    return None


def history_row(run: Optional[RecurringJobHistory]) -> Optional[Dict[str, Any]]:
    """One run as RecurringJobHistoryTransformer writes it: success, message, created_at."""
    if run is None:
        return None
    return {"success": run.success, "message": run.message, "created_at": run.created_at}


def describe_job(
    job: RecurringJob,
    *,
    tz_name: Optional[str],
    job_keys: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """One job as the tools return it, with the zone its times are read in.

    Explicit nulls are kept on purpose: a paused job's next_run is null, and an
    absent key would read as "this tool cannot see it" (root CLAUDE.md, rule 34).
    """
    reading = reading_from_job(job)
    data = job.data if isinstance(job.data, dict) else {}
    type_id = job.recurring_job_type_id
    job_type = None
    if job_keys is not None:
        try:
            job_type = job_keys.get(int(type_id))
        except (TypeError, ValueError):
            job_type = None

    described: Dict[str, Any] = {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "job_type": job_type or type_id,
        "owner": job_owner(job),
        "data": data,
        "start_date": job.start_date[:10] if isinstance(job.start_date, str) else job.start_date,
        "end_date": job.end_date[:10] if isinstance(job.end_date, str) else job.end_date,
        "max_occurrences": job.max_occurrences,
        "schedule": clean_schedule(job.schedule),
        "times_are_read_in": zone_label(reading, tz_name),
        "last_run": history_row(job.last_run),
    }
    if job.description:
        described["description"] = job.description
    if reading == READING_ORGANIZATION:
        described["next_run"] = job.next_occurrence
        described["next_run_local"] = local_string(job.next_occurrence, tz_name)
        described["completed_runs"] = job.completed_occurrences
        described["last_ran_at"] = job.last_occurred_on
    else:
        described["next_run"] = None
        described["next_run_note"] = "This Tallyfy server does not report a job's next run."
    return described
