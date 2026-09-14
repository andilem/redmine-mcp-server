"""Easy Redmine attendance: read, record, correct, delete and approve.

Registered only when ``REDMINE_EASY_ENABLED=true``. Attendance is an Easy
Redmine module (``easy_attendances``); a stock Redmine has neither the
endpoints nor the table, so on one of those these tools could only fail.

Attendance is not time tracking. ``list_time_entries`` reports *spent time*
booked against issues; these tools report *presence* -- arrival, departure
and the activity that describes it (at work, home office, holiday, sick).
An instance can have both for the same day, and the numbers need not agree.

Unlike sprints, this needs no database detour: Easy serves
``/easy_attendances.json`` for reading and writing. Three things about that
API shape the code:

- **Filtering goes through Easy Query**, whose parameter syntax is not part
  of the published spec. The filters are sent, and then applied again to
  what came back. A server that ignores an unknown filter therefore returns
  fewer rows, never wrong ones, and the answer says when that happened.
- **The approval endpoint is undocumented.** ``POST
  /easy_attendances/approval_save.json`` is in the spec with a summary and a
  response, and no request body at all. What this tool sends is therefore a
  reconstruction, so every approval is verified by reading the records back
  and comparing ``approval_status``; an unchanged record is reported as
  unconfirmed rather than as success.
- **``approval_status`` is an integer the spec gets wrong.** It documents
  an unnamed enum of ``1..6``; the column on this instance holds ``NULL``,
  ``0``, ``1`` (open), ``2`` (approved) and a rare, unexplained ``3``. The
  raw number is always passed through, ``_APPROVAL_LABELS`` adds a readable
  ``approval_state`` for the two that are established, and
  ``_DECISION_STATUS`` is the only place a number may be written from --
  rejection is not mapped, so it is refused rather than guessed.
"""

import logging
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field
from redminelib.exceptions import ResourceNotFoundError

from .._decorators import ActionMode, action_dispatch
from .._easy_api import as_dict, describe, easy_request, first_list
from .._env import _is_easy_enabled, _is_read_only_mode
from .._errors import _READ_ONLY_ERROR, _handle_redmine_error
from .._offload import in_thread, offloaded
from .._serialization import wrap_insecure_content
from .._validation import _is_positive_int
from ..server import mcp

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100

# A page to read at a time and a ceiling on how far to walk while the
# filters are being re-applied locally. Same shape as list_easy_sprints:
# the window the server answers in and the window the caller asked for are
# not the same thing.
_PAGE_SIZE = 100
_MAX_SCAN_ROWS = 1000

# What approval_status means on this deployment. The API's own enum
# (1..6, unnamed) does not describe the data: the column holds NULL, 0, 1, 2
# and 3 here. 1 and 2 were established by correlating the column with
# approved_by_id, approved_at and the activity's approval_required; 0 and
# NULL are legacy rows that never went through the workflow. 3 is rare and
# unexplained, which is why it is not mapped: it is the likely "rejected",
# but likely is not enough to write into someone's working time.
_APPROVAL_LABELS: Dict[int, str] = {
    1: "open",
    2: "approved",
}

# The numbers approve_easy_attendances is allowed to send. A decision with
# no number here is refused rather than guessed, and every response repeats
# the raw number, so a wrong entry is visible instead of silent.
_DECISION_STATUS: Dict[str, Optional[int]] = {
    "approve": 2,
    "reject": None,
}

_UNMAPPED_DECISION = {
    "error": (
        "This server does not know which approval_status number rejects an "
        "attendance on this Easy Redmine."
    ),
    "hint": (
        "Easy documents the field as an unnamed enum, and this instance's "
        "column does not match it: it holds NULL, 0, 1 (open), 2 (approved) "
        "and a rare 3 that nobody has explained. 3 is the likely rejection, "
        "but it is unverified. To settle it, reject one record in the web "
        "interface and read it back with list_easy_attendances, then have "
        "the operator record the number in _DECISION_STATUS in "
        "tools/easy_attendances.py. Approving works already."
    ),
    "code": "APPROVAL_STATUS_UNKNOWN",
}


def _attendance_to_dict(row: Any) -> Dict[str, Any]:
    """Convert one ``easy_attendance`` payload to a serializable dict.

    The payload is already plain JSON -- these come from raw requests, not
    from python-redmine resources -- so this is a projection rather than a
    conversion. Location and IP fields are dropped: they answer "where was
    this person" rather than "were they at work", and nothing a caller asks
    of this tool needs them.
    """
    row = as_dict(row)
    activity = as_dict(row.get("easy_attendance_activity"))
    return {
        "id": row.get("id"),
        "user": _ref(row.get("user")),
        "arrival": row.get("arrival"),
        "departure": row.get("departure"),
        "hours": row.get("hours"),
        "activity": (
            {
                "id": activity.get("id"),
                "name": activity.get("name"),
                "at_work": activity.get("at_work"),
            }
            if activity
            else None
        ),
        # Free text the person typed. Wrapped like every other user-authored
        # string that reaches a model.
        "description": wrap_insecure_content(row.get("description") or ""),
        "approval_status": row.get("approval_status"),
        # The raw number stays, because the mapping is this instance's and
        # the column holds values the API's own enum does not describe.
        "approval_state": _approval_state(row.get("approval_status")),
        "need_approve": row.get("need_approve"),
        "approved_by": _ref(row.get("approved_by")),
        "approved_at": row.get("approved_at"),
        "locked": row.get("locked"),
        "time_entry_id": as_dict(row.get("time_entry")).get("id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _approval_state(value: Any) -> Optional[str]:
    """The readable name for an ``approval_status``, where one is known.

    Easy's schema types the field as a string whose enum members are
    integers, so the wire value could be either; both are accepted rather
    than betting on one. Anything unmapped answers ``None``, which keeps
    the rare 3 and the legacy 0/NULL rows unnamed instead of invented.
    """
    try:
        return _APPROVAL_LABELS.get(int(value))
    except (TypeError, ValueError):
        return None


def _ref(value: Any) -> Optional[Dict[str, Any]]:
    """``{id, name}`` for an Easy association, or ``None``."""
    ref = as_dict(value)
    if not ref:
        return None
    return {"id": ref.get("id"), "name": ref.get("name")}


def _rows_of(payload: Any) -> List[Any]:
    """The ``easy_attendances`` array of a list response."""
    rows = as_dict(payload).get("easy_attendances")
    return rows if isinstance(rows, list) else []


def _day(value: Any) -> str:
    """The ``YYYY-MM-DD`` prefix of an API timestamp."""
    return str(value or "")[:10]


def _matches(
    row: Dict[str, Any],
    *,
    user_id: Optional[int],
    from_date: Optional[str],
    to_date: Optional[str],
) -> bool:
    """Whether a row satisfies the filters that were asked for.

    Applied to what the server returned, because Easy Query's parameter
    syntax is not published and a filter it does not recognize is ignored
    rather than refused. Comparing dates as strings is sound here: the API
    formats them ``YYYY-MM-DDTHH:MM:SSZ``, which orders lexicographically
    for the day prefix this compares.
    """
    if user_id is not None and as_dict(row.get("user")).get("id") != user_id:
        return False
    arrival_day = _day(row.get("arrival"))
    if from_date and (not arrival_day or arrival_day < from_date):
        return False
    if to_date and (not arrival_day or arrival_day > to_date):
        return False
    return True


def _valid_date(value: Optional[str]) -> bool:
    if value is None:
        return True
    parts = str(value).split("-")
    return (
        len(parts) == 3
        and all(p.isdigit() for p in parts)
        and len(parts[0]) == 4
        and len(parts[1]) == 2
        and len(parts[2]) == 2
    )


async def list_easy_attendances(
    user_id: Optional[int] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: Annotated[int, Field(ge=1, le=_MAX_LIMIT)] = 25,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> Dict[str, Any]:
    """List Easy Redmine attendance records (presence, not booked time).

    Reads ``GET /easy_attendances.json``. Which records come back is
    Redmine's decision: a user without the right to see other people's
    attendance gets their own, whatever is asked for here.

    Args:
        user_id: Keep only this user's records. Redmine's numeric user id,
            not a login; ``get_current_user`` reports the caller's own.
        from_date: ``YYYY-MM-DD``; keep records whose *arrival* falls on or
            after this day.
        to_date: ``YYYY-MM-DD``; keep records whose *arrival* falls on or
            before this day.
        limit: Maximum records to return (default 25, max 100).
        offset: Records to skip, for paging. It counts answered records,
            not rows read, so a page is never short because rows were
            filtered out behind it.

    Returns:
        ``{"attendances": [...]}``, newest arrival first, each record as
        ``{id, user, arrival, departure, hours, activity, description,
        approval_status, need_approve, approved_by, approved_at, locked,
        approval_state, time_entry_id, created_at, updated_at}``.
        ``approval_status`` is Easy's raw number and ``approval_state``
        names it where this instance's meaning is established (``open``,
        ``approved``); it is ``None`` for the values that are not, which
        includes the rare ``3`` and the legacy ``0``/``NULL`` rows. On
        failure, a dict with an ``"error"`` key.

    Note:
        Attendance is not the same as spent time. Use ``list_time_entries``
        for hours booked against issues.
    """
    if user_id is not None and not _is_positive_int(user_id):
        return {"error": "user_id must be a positive integer."}
    for name, value in (("from_date", from_date), ("to_date", to_date)):
        if not _valid_date(value):
            return {"error": f"{name} must be a date as YYYY-MM-DD."}
    if from_date and to_date and from_date > to_date:
        return {"error": "from_date must not be after to_date."}

    def _run() -> Dict[str, Any]:
        kept: List[Dict[str, Any]] = []
        wanted = offset + limit
        scanned = 0
        dropped = 0
        truncated = False

        while len(kept) < wanted:
            if scanned >= _MAX_SCAN_ROWS:
                truncated = True
                break
            params: Dict[str, Any] = {
                "limit": _PAGE_SIZE,
                "offset": scanned,
                "sort": "arrival:desc",
            }
            if user_id is not None or from_date or to_date:
                # Easy Query's own switch. The filters below are its
                # documented shorthand; anything it does not recognize it
                # drops, which _matches then compensates for.
                params["set_filter"] = 1
                if user_id is not None:
                    params["user_id"] = user_id
                if from_date and to_date:
                    params["arrival"] = f"><{from_date}|{to_date}"
                elif from_date:
                    params["arrival"] = f">={from_date}"
                elif to_date:
                    params["arrival"] = f"<={to_date}"

            try:
                payload = easy_request("get", "easy_attendances.json", params=params)
            except Exception as exc:
                return _handle_redmine_error(exc, "listing Easy Redmine attendances")

            rows = _rows_of(payload)
            if not rows:
                break
            scanned += len(rows)

            for row in rows:
                if len(kept) >= wanted:
                    break
                row = as_dict(row)
                if not _matches(
                    row, user_id=user_id, from_date=from_date, to_date=to_date
                ):
                    dropped += 1
                    continue
                kept.append(_attendance_to_dict(row))

            if len(rows) < _PAGE_SIZE:
                break

        result: Dict[str, Any] = {"attendances": kept[offset : offset + limit]}
        if dropped:
            result["note"] = (
                f"{dropped} record(s) the server returned did not match the "
                "requested filters and were dropped here. This Easy Redmine "
                "does not apply them server-side, so paging reads more rows "
                "than it answers."
            )
        if truncated:
            result["truncated"] = True
            result.setdefault("note", "")
            result["note"] = (
                result["note"] + " " if result["note"] else ""
            ) + f"Stopped after reading {scanned} records; narrow the range."
        return result

    return await in_thread(_run)


def _activities() -> List[Dict[str, Any]]:
    """The instance's attendance activities, as ``{id, name, ...}``.

    ``GET /easy_entity_activities.json``. The ids are per-instance -- ours
    runs from Office and Home office through Vacation, Flexday and On-Call
    Service -- so they are read rather than hardcoded, and a caller that has
    to name one gets the current list instead of a number to guess.

    ``use_specify_time`` is worth passing on: it is false for the full-day
    activities (holiday, compassionate leave), where an arrival time is
    meaningless. ``approval_required`` says whether a record will wait for a
    decision at all.

    Best effort: a failing read answers with an empty list, because this
    only ever enriches an error that already stands on its own.
    """
    try:
        payload = easy_request(
            "get", "easy_entity_activities.json", params={"limit": 100}
        )
    except Exception as exc:  # noqa: BLE001 -- enrichment, never the answer
        logger.debug("Could not read attendance activities: %s", exc)
        return []
    activities = []
    for row in first_list(payload):
        row = as_dict(row)
        if row.get("id") is None:
            continue
        activities.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "at_work": row.get("at_work"),
                "approval_required": row.get("approval_required"),
                "use_specify_time": row.get("use_specify_time"),
            }
        )
    return activities


async def _create_attendance_action(
    user_id: Optional[int] = None,
    activity_id: Optional[int] = None,
    arrival: Optional[str] = None,
    departure: Optional[str] = None,
    description: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """POST /easy_attendances.json. Answers 201 with the created record."""
    missing = [
        name
        for name, value in (
            ("user_id", user_id),
            ("activity_id", activity_id),
            ("arrival", arrival),
        )
        if value is None
    ]
    if missing:
        refusal: Dict[str, Any] = {
            "error": f"create needs {', '.join(missing)}.",
            "hint": (
                "user_id is the Redmine user the record belongs to "
                "(get_current_user reports your own), activity_id is an "
                "Easy attendance activity, and arrival is an ISO 8601 "
                "timestamp such as 2026-09-14T08:00:00Z."
            ),
        }
        if activity_id is None:
            # The ids are per-instance and there is no tool that lists them,
            # so the refusal carries them rather than sending the caller to
            # guess a number. Best effort: a failed read just omits the list.
            activities = await in_thread(_activities)
            if activities:
                refusal["activities"] = activities
        return refusal
    if not _is_positive_int(user_id) or not _is_positive_int(activity_id):
        return {"error": "user_id and activity_id must be positive integers."}

    body: Dict[str, Any] = {
        "user_id": user_id,
        "easy_attendance_activity_id": activity_id,
        "arrival": arrival,
    }
    if departure is not None:
        body["departure"] = departure
    if description is not None:
        body["description"] = description

    def _run() -> Dict[str, Any]:
        try:
            payload = easy_request(
                "post", "easy_attendances.json", data={"easy_attendance": body}
            )
        except Exception as exc:
            return _handle_redmine_error(exc, "creating an Easy Redmine attendance")
        record = as_dict(payload).get("easy_attendance")
        if not as_dict(record).get("id"):
            return {
                "error": (
                    "The server accepted the attendance but did not return "
                    "it, so it cannot be confirmed."
                ),
                "code": "CREATE_UNCONFIRMED",
                "hint": (
                    "Check with list_easy_attendances before creating it "
                    "again. Response was: " + describe(payload)
                ),
            }
        return {"attendance": _attendance_to_dict(record), "created": True}

    return await in_thread(_run)


async def _update_attendance_action(
    attendance_id: Optional[int] = None,
    user_id: Optional[int] = None,
    activity_id: Optional[int] = None,
    arrival: Optional[str] = None,
    departure: Optional[str] = None,
    description: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """PUT /easy_attendances/{id}.json, then read the record back.

    The PUT answers 200 with no promised body, so the result reported here
    is a fresh read rather than an echo of what was sent.
    """
    if not _is_positive_int(attendance_id):
        return {"error": "update needs attendance_id, a positive integer."}

    body: Dict[str, Any] = {}
    if user_id is not None:
        body["user_id"] = user_id
    if activity_id is not None:
        body["easy_attendance_activity_id"] = activity_id
    if arrival is not None:
        body["arrival"] = arrival
    if departure is not None:
        body["departure"] = departure
    if description is not None:
        body["description"] = description
    if not body:
        return {
            "error": "update needs at least one field to change.",
            "hint": (
                "Pass activity_id, arrival, departure, description or " "user_id."
            ),
        }

    def _run() -> Dict[str, Any]:
        path = f"easy_attendances/{attendance_id}.json"
        try:
            easy_request("put", path, data={"easy_attendance": body})
        except Exception as exc:
            return _handle_redmine_error(exc, "updating an Easy Redmine attendance")
        try:
            payload = easy_request("get", path)
        except Exception as exc:
            logger.warning("Attendance %s updated but not readable", attendance_id)
            return _handle_redmine_error(
                exc, "reading back the updated Easy Redmine attendance"
            )
        record = as_dict(payload).get("easy_attendance")
        return {"attendance": _attendance_to_dict(record), "updated": True}

    return await in_thread(_run)


@action_dispatch(
    {
        "create": ActionMode.WRITE,
        "update": ActionMode.WRITE,
    }
)
async def manage_easy_attendance(
    action: str,
    attendance_id: Optional[int] = None,
    user_id: Optional[int] = None,
    activity_id: Optional[int] = None,
    arrival: Optional[str] = None,
    departure: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Record or correct one Easy Redmine attendance (presence).

    Deleting is a separate tool, ``delete_easy_attendance``, and approving
    another one, ``approve_easy_attendances``, so a deployment can offer
    attendance without offering either.

    Args:
        action: One of ``create``, ``update``.
        attendance_id: Record to change. Required for ``update``.
        user_id: Whose attendance this is. Required for ``create``. Writing
            another user's record needs the right to do so in Redmine.
        activity_id: Easy attendance activity -- at work, home office,
            holiday, sick. Required for ``create``. The ids are
            per-instance; a ``create`` that omits this answers with the
            instance's current list rather than expecting a guess.
        arrival: Start, ISO 8601 (``2026-09-14T08:00:00Z``). Required for
            ``create``.
        departure: End, ISO 8601. A record without one is an open,
            still-running attendance.
        description: Free-text note.

    Returns:
        ``{"attendance": {...}, "created"|"updated": True}``. On a create
        the server did not confirm, ``code: CREATE_UNCONFIRMED`` instead of
        a made-up record. On error, ``{"error": "..."}``.

        Blocked in read-only mode (``REDMINE_MCP_READ_ONLY=true``).

    Note:
        A record Easy has locked, or one already approved, is refused by
        the server rather than by this tool.
    """
    return {
        "create": _create_attendance_action,
        "update": _update_attendance_action,
    }


@offloaded
def delete_easy_attendance(
    attendance_id: Optional[int] = None,
    confirm_delete: bool = False,
) -> Dict[str, Any]:
    """Delete an attendance record via ``DELETE /easy_attendances/{id}``.

    Deletion is **irreversible**. Like the other destructive tools here,
    this one refuses unless ``confirm_delete=True``, and the refusal
    carries what would be lost.

    Args:
        attendance_id: Record to delete.
        confirm_delete: When ``False`` (default), the tool refuses and
            returns a preview. Pass ``True`` to actually delete.

    Returns:
        On refusal: ``{"error", "code": "CONFIRMATION_REQUIRED",
        "attendance": {...}}``. On success: ``{"deleted": True,
        "attendance_id": n}``. On error, ``{"error": "..."}``.

        Blocked in read-only mode (``REDMINE_MCP_READ_ONLY=true``).
    """
    if _is_read_only_mode():
        return dict(_READ_ONLY_ERROR)
    if not _is_positive_int(attendance_id):
        return {"error": "attendance_id must be a positive integer."}

    path = f"easy_attendances/{attendance_id}.json"
    try:
        payload = easy_request("get", path)
    except Exception as exc:
        return _handle_redmine_error(exc, "reading the Easy Redmine attendance")
    record = _attendance_to_dict(as_dict(payload).get("easy_attendance"))

    if not confirm_delete:
        return {
            "error": (
                f"Deleting attendance {attendance_id} is irreversible. "
                "Pass confirm_delete=True to proceed."
            ),
            "code": "CONFIRMATION_REQUIRED",
            "attendance": record,
        }

    try:
        easy_request("delete", path)
    except Exception as exc:
        return _handle_redmine_error(exc, "deleting the Easy Redmine attendance")
    return {"deleted": True, "attendance_id": attendance_id, "attendance": record}


@offloaded
def approve_easy_attendances(
    attendance_ids: Optional[List[int]] = None,
    decision: str = "approve",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Approve or reject attendance records someone submitted.

    Calls ``POST /easy_attendances/approval_save.json``, which the API
    documents with a summary and a response and **no request body**. What
    goes on the wire is therefore reconstructed, so this tool reads every
    record back afterwards and reports the resulting ``approval_status``.
    A record whose status did not change is reported as unconfirmed, never
    as approved.

    Args:
        attendance_ids: Records to decide on. At most 50 per call.
        decision: ``approve`` (default) or ``reject``.
        confirm: Must be ``True``. Approving is someone else's working time
            being signed off, so it is never the accidental result of a
            half-specified call.

    Returns:
        ``{"results": [{attendance_id, approval_status_before,
        approval_status_after, changed}], "confirmed": bool}``. When the
        endpoint answered but nothing changed, ``code:
        APPROVAL_UNCONFIRMED`` with the raw response. On error,
        ``{"error": "..."}``.

        Blocked in read-only mode (``REDMINE_MCP_READ_ONLY=true``).

    Note:
        ``approval_status`` numbers are Easy's own and are not named
        anywhere in its API. This deployment's mapping lives in
        ``_DECISION_STATUS``; until an operator fills it in, the tool
        refuses rather than sending a number nobody verified.
    """
    if _is_read_only_mode():
        return dict(_READ_ONLY_ERROR)

    ids = [i for i in (attendance_ids or []) if _is_positive_int(i)]
    if not ids or len(ids) != len(attendance_ids or []):
        return {"error": "attendance_ids must be a list of positive integers."}
    if len(ids) > 50:
        return {"error": "At most 50 attendance records per call."}
    if decision not in _DECISION_STATUS:
        return {
            "error": (
                f"Invalid decision {decision!r}. "
                f"Allowed: {', '.join(sorted(_DECISION_STATUS))}."
            )
        }
    if not confirm:
        return {
            "error": (
                f"This would {decision} {len(ids)} attendance record(s). "
                "Pass confirm=True to proceed."
            ),
            "code": "CONFIRMATION_REQUIRED",
        }

    target = _DECISION_STATUS[decision]
    if target is None:
        return dict(_UNMAPPED_DECISION)

    before: Dict[int, Any] = {}
    for attendance_id in ids:
        try:
            payload = easy_request("get", f"easy_attendances/{attendance_id}.json")
        except ResourceNotFoundError:
            return {
                "error": f"Attendance {attendance_id} does not exist.",
                "code": "NOT_FOUND",
            }
        except Exception as exc:
            return _handle_redmine_error(exc, "reading attendances before approval")
        before[attendance_id] = as_dict(as_dict(payload).get("easy_attendance")).get(
            "approval_status"
        )

    try:
        response = easy_request(
            "post",
            "easy_attendances/approval_save.json",
            data={"ids": ids, "approval_status": target},
        )
    except Exception as exc:
        return _handle_redmine_error(exc, "approving Easy Redmine attendances")

    results: List[Dict[str, Any]] = []
    for attendance_id in ids:
        try:
            payload = easy_request("get", f"easy_attendances/{attendance_id}.json")
            after = as_dict(as_dict(payload).get("easy_attendance")).get(
                "approval_status"
            )
        except Exception:  # noqa: BLE001 -- the decision already happened
            after = None
        results.append(
            {
                "attendance_id": attendance_id,
                "approval_status_before": before[attendance_id],
                "approval_status_after": after,
                "changed": after is not None and after != before[attendance_id],
            }
        )

    confirmed = all(r["changed"] for r in results)
    result: Dict[str, Any] = {
        "results": results,
        "decision": decision,
        "approval_status_sent": target,
        "confirmed": confirmed,
    }
    if not confirmed:
        result["code"] = "APPROVAL_UNCONFIRMED"
        result["error"] = (
            "The endpoint answered, but the records did not change. The "
            "request shape for approval_save is not documented by Easy, so "
            "this server's reconstruction of it may not match this version."
        )
        result["response"] = describe(response)
    return result


# Registered on the MCP surface only when Easy Redmine support is on, the
# same shape as list_easy_sprints. A stock Redmine serves none of these
# endpoints.
if _is_easy_enabled():
    list_easy_attendances = mcp.tool()(list_easy_attendances)
    manage_easy_attendance = mcp.tool()(manage_easy_attendance)
    delete_easy_attendance = mcp.tool()(delete_easy_attendance)
    approve_easy_attendances = mcp.tool()(approve_easy_attendances)
