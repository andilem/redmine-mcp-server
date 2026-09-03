"""Read-only database access for what Easy Redmine's REST API does not expose.

Easy Redmine serves no sprint endpoint. ``/easy_sprints.json`` answers 403
even with a valid API key, and the instance's own ``/easy_swagger.json``
lists no sprint path at all, while ``easy_sprint_id`` is a documented
attribute of ``IssueApiRequest``. So a sprint can be *set* over HTTP but its
name can never be *looked up* over HTTP, which leaves "put issue X in sprint
PGMS 26-34" unanswerable without reading the table directly.

This module is the one exception to "everything goes through the REST API",
and it is kept as narrow as an exception should be:

- **Named queries only.** There is deliberately no "run this SQL" entry
  point. One would hand every caller a read of ``users.hashed_password``,
  every stored API key and every private issue; read-only credentials do
  not help against that, because reading is the whole problem.
- **The caller scopes the result.** A database read bypasses Redmine's
  authorization completely, so this layer returns rows and
  :mod:`.tools.easy_sprints` decides which of them the caller may see,
  using the caller's own API key.
- **The session is READ ONLY.** Set on connect, so credentials that turn
  out to be writable still cannot write through here.
- **Timeouts are explicit.** A hung database must not hang the MCP server.
"""

import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from ._serialization import wrap_insecure_content

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 5
_READ_TIMEOUT_SECONDS = 10

# Columns of easy_sprints worth returning. `easy_external_id` and the
# created_at/updated_at pair are left out: they answer nothing a caller asked.
_SPRINT_COLUMNS = (
    "s.id",
    "s.name",
    "s.start_date",
    "s.due_date",
    "s.closed",
    "s.cross_project",
    "s.capacity",
    "s.goal",
    "s.version_id",
    "s.project_id",
)


def database_url() -> Optional[str]:
    """The configured read-only DSN, or ``None``."""
    raw = os.getenv("REDMINE_EASY_DB_URL", "").strip()
    return raw or None


def is_configured() -> bool:
    return database_url() is not None


def _connection_params() -> Dict[str, Any]:
    """Split the DSN into pymysql keyword arguments.

    Accepts ``mysql://`` and ``mysql+pymysql://`` so a URL copied from an
    SQLAlchemy config works unchanged.
    """
    url = database_url()
    if url is None:
        raise RuntimeError(
            "REDMINE_EASY_DB_URL is not set. It is required for sprint "
            "lookups, which Easy Redmine's REST API does not offer."
        )
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme not in ("mysql", "mariadb"):
        raise RuntimeError(
            f"REDMINE_EASY_DB_URL must be a mysql:// or mariadb:// URL, got "
            f"{parsed.scheme!r}."
        )
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise RuntimeError("REDMINE_EASY_DB_URL names no database.")
    if not parsed.hostname:
        # No silent fallback to localhost: inside a container that is the
        # container itself, and the resulting "connection refused" reads
        # like a database outage rather than a malformed URL.
        raise RuntimeError(
            "REDMINE_EASY_DB_URL names no host. Note that a containerized "
            "server reaches neither localhost nor 127.0.0.1 of its host: use "
            "the database host's name or address as seen from inside the "
            "container."
        )
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": "utf8mb4",
        "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
        "read_timeout": _READ_TIMEOUT_SECONDS,
    }


def _select(sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
    """Run one parameterized SELECT and return its rows as dicts.

    Private on purpose: ``sql`` is built by this module, never by a caller.
    """
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "Sprint lookups need the PyMySQL driver, which is an optional "
            "dependency. Install the package with its 'easy' extra "
            "(pip install 'redmine-mcp-server[easy]')."
        ) from exc

    connection = pymysql.connect(cursorclass=DictCursor, **_connection_params())
    try:
        with connection.cursor() as cursor:
            # Belt and braces: even write credentials cannot write from here.
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(sql, tuple(params))
            return list(cursor.fetchall())
    finally:
        connection.close()


def _as_iso(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _row_to_sprint(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "start_date": _as_iso(row["start_date"]),
        "due_date": _as_iso(row["due_date"]),
        "closed": bool(row["closed"]),
        "cross_project": bool(row["cross_project"]),
        "capacity": row["capacity"],
        # The goal is prose a user typed, and it arrives as HTML that can
        # carry invisible text. Every other user-authored field on this
        # server is wrapped before it reaches an LLM; coming from the
        # database rather than the REST API changes nothing about that.
        "goal": wrap_insecure_content(row["goal"]),
        "version_id": row["version_id"],
        "project_id": row["project_id"],
    }


def build_sprint_query(
    name: Optional[str] = None,
    closed: Optional[bool] = False,
    active_on: Optional[str] = None,
    project_id: Optional[int] = None,
    limit: int = 25,
    offset: int = 0,
) -> Tuple[str, List[Any]]:
    """Build the sprint SELECT and its parameters.

    Split out from :func:`fetch_sprints` so the SQL can be asserted on
    without a database. Every caller-supplied value becomes a placeholder;
    nothing is interpolated into the statement.

    ``active_on`` means "the sprint was running on that date", i.e.
    ``start_date <= d <= due_date``. A sprint with either date NULL is not
    running on any date and drops out -- which is the honest answer, since
    Easy Redmine cannot place it on a calendar either.
    """
    where: List[str] = []
    params: List[Any] = []

    if closed is not None:
        where.append("s.closed = %s")
        params.append(1 if closed else 0)
    if active_on:
        where.append("s.start_date <= %s AND s.due_date >= %s")
        params.extend([active_on, active_on])
    if name:
        where.append("s.name LIKE %s")
        params.append(f"%{name}%")
    if project_id is not None:
        # cross_project sprints and project-less ones are not tied to the
        # project asked about but do apply to it, so they stay in.
        where.append(
            "(s.project_id = %s OR s.cross_project = 1 OR s.project_id IS NULL)"
        )
        params.append(project_id)

    sql = (
        f"SELECT {', '.join(_SPRINT_COLUMNS)} "
        "FROM easy_sprints s "
        + (f"WHERE {' AND '.join(where)} " if where else "")
        + "ORDER BY s.due_date IS NULL, s.due_date DESC, s.id DESC "
        "LIMIT %s OFFSET %s"
    )
    params.extend([limit, offset])
    return sql, params


def fetch_sprints(**kwargs: Any) -> List[Dict[str, Any]]:
    """Sprints matching the filters, newest due date first.

    Rows only -- the caller is responsible for hiding the ones its user may
    not see. See this module's docstring for why that split exists.
    """
    sql, params = build_sprint_query(**kwargs)
    rows = _select(sql, params)
    logger.debug("Sprint query returned %d row(s).", len(rows))
    return [_row_to_sprint(row) for row in rows]
