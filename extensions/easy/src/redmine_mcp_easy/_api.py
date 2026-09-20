"""Raw calls to the endpoints Easy Redmine adds to Redmine's REST API.

python-redmine models core Redmine. Easy's own entities have no resource
class, so there is nothing to hang a ``redmine.easy_attendance.filter()``
on. The engine underneath is usable directly, though, and going through it
rather than through ``requests`` keeps everything that matters: the session
with the caller's API key or bearer token, the configured timeout, the SSL
settings, the redirect warning, and -- most usefully -- the mapping from
HTTP status to the ``redminelib`` exceptions that ``handle_redmine_error``
already knows how to phrase.

Two rough edges of that engine are smoothed here:

- A 422 makes python-redmine read ``response.json()["errors"]``. Easy's
  endpoints do not all answer in that shape, and the ``KeyError`` that then
  escapes says nothing about what went wrong. It is turned into a
  ``ValidationError`` carrying the body instead.
- A 200 or 204 with an empty body returns ``True``. Callers here want
  "nothing came back" to be falsy-but-explicit, so it is normalized to
  ``None``.
"""

import json
import logging
from typing import Any, Dict, Optional

from redminelib.exceptions import ValidationError

from redmine_mcp_server.extensions import get_redmine_client

logger = logging.getLogger(__name__)


def easy_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Call ``path`` on the configured Redmine and return the parsed body.

    Args:
        method: ``get``, ``post``, ``put`` or ``delete``.
        path: Path below the Redmine root, e.g. ``easy_attendances.json``.
        params: Query string parameters.
        data: Body for a write; JSON-encoded by the engine.

    Returns:
        The decoded JSON body, or ``None`` when the response had no body
        (a 204, or a 200 that answered empty).

    Raises:
        redminelib.exceptions.*: the usual mapping -- ``AuthError`` (401),
            ``ForbiddenError`` (403), ``ResourceNotFoundError`` (404),
            ``ValidationError`` (422), ``ServerError`` (500).
    """
    client = get_redmine_client()
    url = f"{str(client.url).rstrip('/')}/{path.lstrip('/')}"

    try:
        result = client.engine.request(method, url, params=params, data=data)
    except KeyError as exc:
        # python-redmine's 422 branch assumes {"errors": [...]}. Easy answers
        # some of its own endpoints differently, and a bare KeyError('errors')
        # would reach the caller as "an unexpected error occurred".
        logger.warning("Easy endpoint %s answered 422 in an unknown shape", path)
        raise ValidationError(
            "The server rejected the request but did not say why "
            f"(HTTP 422, no 'errors' in the body; missing key {exc})."
        ) from exc

    if result is True:  # 200/204 with an empty body
        return None
    return result


def as_dict(value: Any) -> Dict[str, Any]:
    """The mapping inside an API response, or ``{}`` for anything else.

    Easy answers a single entity as ``{"easy_attendance": {...}}`` but is
    not consistent about it across endpoints and versions, so every read of
    a nested key goes through this rather than assuming the wrapper is
    there.
    """
    return value if isinstance(value, dict) else {}


def first_list(value: Any) -> list:
    """The first array among a response's top-level values.

    Easy wraps collections under a key named after the entity, and the name
    is not always the one the path suggests -- attendance activities come
    back from ``/easy_entity_activities.json``. Reading the first list is
    stable across that naming, and these responses carry exactly one.
    """
    for item in as_dict(value).values():
        if isinstance(item, list):
            return item
    return []


def describe(value: Any, limit: int = 300) -> str:
    """A short, safe rendering of an unexpected body, for error messages."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]
