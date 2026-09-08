"""The sprint lookup that Easy Redmine's REST API is missing.

Registered only when ``REDMINE_EASY_ENABLED=true``, like the admin-gated
cleanup tool: on a stock Redmine there are no sprints to list.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from redminelib.exceptions import ForbiddenError, ResourceNotFoundError

from .._client import _get_redmine_client
from .._easy_db import fetch_sprints, is_configured
from .._env import _is_easy_enabled
from .._errors import _handle_redmine_error
from .._offload import in_thread
from ..server import mcp

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100

# Rows are filtered for visibility here rather than in SQL, so the database
# window and the answer's window are not the same thing. These bound the
# walk: a page to read at a time, and a ceiling on how deep to go before
# answering with what was found.
_PAGE_SIZE = 50
_MAX_SCAN_ROWS = 500


def _resolve_project(project_id: int, names: Dict[int, Optional[str]]) -> None:
    """Record the project's name in ``names``, or ``None`` when it is not
    the caller's to see.

    The database read behind this tool bypasses Redmine's authorization, so
    visibility is re-established here, with the caller's key rather than the
    database's. One GET per distinct project sounds expensive but is not:
    sprints cluster on a handful of projects, and the alternative -- pulling
    the whole project list -- costs far more on an instance with a thousand
    of them.

    Only "you may not have this" counts as an answer. An expired key or an
    unreachable Redmine is raised instead, because a silently empty sprint
    list would send the caller hunting for a missing database connection
    that is not missing.
    """
    if project_id in names:
        return
    client = _get_redmine_client()
    try:
        project = client.project.get(project_id)
    except (ForbiddenError, ResourceNotFoundError):
        # Forbidden, gone, archived -- all mean "not this caller's".
        logger.debug("Project %s not visible to the caller.", project_id)
        names[project_id] = None
        return
    names[project_id] = str(getattr(project, "name", "") or "")


def _place(
    sprint: Dict[str, Any], names: Dict[int, Optional[str]]
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Whether the caller may see ``sprint``, and the project to report for
    it. Consumes the row's ``project_id``.
    """
    project_id = sprint.pop("project_id")
    if project_id is None:
        return True, None
    name = names.get(project_id)
    if name is not None:
        return True, {"id": project_id, "name": name}
    # The owning project is not the caller's. A cross-project sprint applies
    # to theirs regardless -- Easy Redmine offers it in every project's
    # sprint picker, and the query keeps it for the same reason -- so it
    # stays, without naming an owner they cannot open. A sprint tied to one
    # project alone is not theirs to see.
    return bool(sprint.get("cross_project")), None


async def list_easy_sprints(
    name: Optional[str] = None,
    active_on: Optional[str] = None,
    closed: Optional[bool] = False,
    project_id: Optional[int] = None,
    limit: int = 25,
    offset: int = 0,
) -> Dict[str, Any]:
    """List Easy Redmine sprints, to turn a sprint name into an id.

    Easy Redmine exposes no sprint endpoint -- ``/easy_sprints.json`` answers
    403 even with a valid API key -- so this reads the ``easy_sprints`` table
    through the read-only connection in ``REDMINE_EASY_DB_URL``. Results are
    then filtered to what the calling user may see, using their own API key,
    because a database read has no permissions of its own.

    The ``id`` is what the other two sprint operations need:

    - which issues are in a sprint:
      ``list_redmine_issues(filters={"easy_sprint_id": id})``
    - move an issue into one:
      ``update_redmine_issue(issue_id, {"easy_sprint_id": id})``

    Args:
        name: Match sprint names containing this text, case-insensitively
            (the column's collation decides). Use it to resolve a name a
            user typed, e.g. ``"26-34"``.
        active_on: ``YYYY-MM-DD``; keep only sprints running on that date
            (``start_date <= date <= due_date``). Pass today's date for
            "the current sprint". Sprints missing either date are never
            running on a date and drop out.
        closed: ``False`` (default) for open sprints, ``True`` for closed
            ones, ``None`` for both.
        project_id: Keep sprints belonging to this project, plus the
            cross-project and project-less ones, which apply everywhere.
        limit: Maximum sprints to return (default 25, max 100).
        offset: Sprints to skip, for paging. It counts answered sprints,
            not table rows, so a page is never short because rows were
            filtered out behind it.

    Returns:
        ``{"sprints": [...]}`` with each sprint as ``{id, name, start_date,
        due_date, closed, cross_project, capacity, goal, version_id,
        project}``, where ``project`` is ``{id, name}`` or ``None``. Newest
        due date first. On failure, a dict with an ``"error"`` key.

    Note:
        Several sprints can be running on the same date -- one per team is
        the normal case -- so a caller resolving "the current sprint" should
        expect a list and say which one it picked.

        ``project`` is ``None`` both for a sprint that belongs to no project
        and for a cross-project one whose owning project the caller cannot
        open -- a team's sprint commonly lives on a parent project that the
        people working in the subproject are not members of. The sprint
        applies to them all the same, so it is listed; the owner's name is
        not.
    """
    if not is_configured():
        return {
            "error": (
                "Sprint lookups need REDMINE_EASY_DB_URL, a read-only "
                "database connection."
            ),
            "hint": (
                "Easy Redmine serves no sprint endpoint (/easy_sprints.json "
                "is 403 even with an API key), so the sprint table is the "
                "only source. Ask the operator to configure it. Setting a "
                "sprint by id works without this."
            ),
            "code": "EASY_DB_NOT_CONFIGURED",
        }

    if limit < 1 or limit > _MAX_LIMIT:
        return {"error": f"limit must be between 1 and {_MAX_LIMIT}."}
    if offset < 0:
        return {"error": "offset must not be negative."}

    def _run() -> Dict[str, Any]:
        names: Dict[int, Optional[str]] = {}
        kept: List[Dict[str, Any]] = []
        wanted = offset + limit
        scanned = 0
        truncated = False

        while len(kept) < wanted:
            if scanned >= _MAX_SCAN_ROWS:
                truncated = True
                break
            try:
                rows = fetch_sprints(
                    name=name,
                    active_on=active_on,
                    closed=closed,
                    project_id=project_id,
                    limit=_PAGE_SIZE,
                    offset=scanned,
                )
            except RuntimeError as exc:
                # Misconfiguration or a missing driver: the message is the
                # point.
                return {"error": str(exc), "code": "EASY_DB_UNAVAILABLE"}
            except Exception as exc:
                logger.warning("Sprint query failed: %s", exc)
                return {
                    "error": f"Could not read Easy Redmine sprints: {exc}",
                    "code": "EASY_DB_ERROR",
                }
            if not rows:
                break
            scanned += len(rows)

            for sprint in rows:
                if len(kept) >= wanted:
                    break
                candidate = sprint.get("project_id")
                if candidate is not None:
                    try:
                        _resolve_project(candidate, names)
                    except Exception as exc:
                        return _handle_redmine_error(
                            exc, "checking project visibility for sprints", {}
                        )
                keep, project_ref = _place(sprint, names)
                if not keep:
                    continue
                sprint["project"] = project_ref
                kept.append(sprint)

            if len(rows) < _PAGE_SIZE:
                break

        result: Dict[str, Any] = {"sprints": kept[offset : offset + limit]}
        if truncated:
            result["note"] = (
                f"Stopped after reading {scanned} sprints; there may be more "
                "beyond them. Narrow the search with name, active_on or "
                "project_id."
            )
        return result

    return await in_thread(_run)


# Registered on the MCP surface only when Easy Redmine support is on, the
# same shape as the admin-gated cleanup tool. A stock Redmine has no
# easy_sprints table, so an always-present tool could only ever fail.
if _is_easy_enabled():
    list_easy_sprints = mcp.tool()(list_easy_sprints)
