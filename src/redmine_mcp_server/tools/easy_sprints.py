"""The sprint lookup that Easy Redmine's REST API is missing.

Registered only when ``REDMINE_EASY_ENABLED=true``, like the admin-gated
cleanup tool: on a stock Redmine there are no sprints to list.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from .._client import _get_redmine_client
from .._easy_db import fetch_sprints, is_configured
from .._env import _is_easy_enabled
from .._errors import _handle_redmine_error
from .._offload import in_thread
from ..server import mcp

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100


def _visible_projects(candidates: Set[int]) -> Dict[int, str]:
    """Which of ``candidates`` the caller's own API key can read.

    The database read behind this tool bypasses Redmine's authorization, so
    visibility is re-established here, with the caller's key rather than the
    database's. One GET per distinct project sounds expensive but is not:
    sprints cluster on a handful of projects, and the alternative -- pulling
    the whole project list -- costs far more on an instance with a thousand
    of them.
    """
    visible: Dict[int, str] = {}
    client = _get_redmine_client()
    for project_id in candidates:
        try:
            project = client.project.get(project_id)
        except Exception:
            # Not found, forbidden, archived -- all mean "not this caller's".
            logger.debug("Project %s not visible to the caller.", project_id)
            continue
        visible[project_id] = str(getattr(project, "name", "") or "")
    return visible


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
    then filtered to the projects the calling user can actually see, using
    their own API key, because a database read has no permissions of its own.

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
            ``cross_project`` and project-less ones, which apply everywhere.
        limit: Maximum sprints to return (default 25, max 100).
        offset: Sprints to skip, for paging.

    Returns:
        ``{"sprints": [...]}`` with each sprint as ``{id, name, start_date,
        due_date, closed, cross_project, capacity, goal, version_id,
        project}``, where ``project`` is ``{id, name}`` or ``None`` for a
        global sprint. Newest due date first. On failure, a dict with an
        ``"error"`` key.

    Note:
        Several sprints can be running on the same date -- one per team is
        the normal case -- so a caller resolving "the current sprint" should
        expect a list and say which one it picked.
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
        try:
            sprints = fetch_sprints(
                name=name,
                active_on=active_on,
                closed=closed,
                project_id=project_id,
                limit=limit,
                offset=offset,
            )
        except RuntimeError as exc:
            # Misconfiguration or a missing driver: the message is the point.
            return {"error": str(exc), "code": "EASY_DB_UNAVAILABLE"}
        except Exception as exc:
            logger.warning("Sprint query failed: %s", exc)
            return {
                "error": f"Could not read Easy Redmine sprints: {exc}",
                "code": "EASY_DB_ERROR",
            }

        candidates = {s["project_id"] for s in sprints if s["project_id"] is not None}
        try:
            visible = _visible_projects(candidates) if candidates else {}
        except Exception as exc:
            return _handle_redmine_error(
                exc, "checking project visibility for sprints", {}
            )

        result: List[Dict[str, Any]] = []
        for sprint in sprints:
            project_ref = sprint.pop("project_id")
            if project_ref is None:
                sprint["project"] = None
            elif project_ref in visible:
                sprint["project"] = {"id": project_ref, "name": visible[project_ref]}
            else:
                # A sprint in a project the caller cannot open is not theirs
                # to see, whatever the database says.
                continue
            result.append(sprint)

        return {"sprints": result}

    return await in_thread(_run)


# Registered on the MCP surface only when Easy Redmine support is on, the
# same shape as the admin-gated cleanup tool. A stock Redmine has no
# easy_sprints table, so an always-present tool could only ever fail.
if _is_easy_enabled():
    list_easy_sprints = mcp.tool()(list_easy_sprints)
