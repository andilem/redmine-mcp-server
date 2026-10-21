"""Meta / introspection tools.

A single ``get_mcp_server_info`` tool that returns the deployed
package version plus a few non-sensitive runtime flags an MCP client
needs in order to detect deployment lag and choose the right call
shape. Surfaced as a tool (not just an HTTP endpoint) so an LLM caller
can check it through the same protocol it already speaks.

What this deliberately does NOT expose:

- ``REDMINE_URL`` / ``REDMINE_API_KEY`` / OAuth secrets -- credentials
  and internal hostnames stay behind the operator-facing config path.
- File system paths (``ATTACHMENTS_DIR``) -- not interesting to a
  caller and faintly leaky.
- The configured ``PUBLIC_HOST`` / ``REDMINE_PUBLIC_URL`` -- these
  could disclose internal routing detail to a caller that doesn't
  need it.

Only the flags that change *call shape* for the caller are exposed:
which auth mode the server is in, which plugin-gated tool families
are enabled, and whether the server is in read-only mode. The version
string and current-user identity are unambiguously safe.
"""

import logging
from typing import Any, Dict, Optional

from .. import __version__
from .._env import SERVER_INFO_PLUGIN_FLAGS, _is_read_only_mode
from .._extension_registry import REGISTERED_EXTENSIONS
from ..server import mcp

logger = logging.getLogger("redmine_mcp_server")


async def _fetch_current_user_info() -> Optional[Dict[str, Any]]:
    """Return {id, login, name} for the calling user, or None on failure.

    Resolves who ``assigned_to_id="me"`` maps to — crucial when a shared
    or robot API key is in use, where "me" is not the human operator.

    Asks ``GET /users/current.json`` through ``_get_redmine_client``, the
    client every tool call uses, so the answer is the identity that call
    runs as: the configured account in ``legacy`` mode, and the caller's
    own key or bearer in the per-user modes. Those carry no server-wide
    credential, so a request built from ``REDMINE_API_KEY`` alone answered
    None there -- or, with a stray key set, named the server account
    instead of the caller. The client is blocking and runs in a worker
    thread; it brings the SSL settings and ``REDMINE_TIMEOUT`` with it.
    ``/my/account.json`` is not reliably available on older Redmine
    instances, ``/users/current.json`` is on 3.x and later.

    This duplicates ``get_current_user`` and has not been consolidated with
    it; that is the only reason it is a separate path, not any permission
    difference. The endpoint is not admin-gated -- see the
    ``get_current_user`` note in ``oauth_scopes.py`` for why.
    """
    from .. import _client
    from .._offload import in_thread

    def _run():
        return _client._get_redmine_client().user.get("current")

    try:
        user = await in_thread(_run)
    except Exception as exc:
        logger.warning("get_mcp_server_info: could not fetch current user: %s", exc)
        return None
    firstname = getattr(user, "firstname", "") or ""
    lastname = getattr(user, "lastname", "") or ""
    return {
        "id": getattr(user, "id", None),
        "login": getattr(user, "login", None),
        "name": f"{firstname} {lastname}".strip() or None,
    }


@mcp.tool()
async def get_mcp_server_info() -> Dict[str, Any]:
    """Return the MCP server's version, enabled-feature flags, and current user.

    Use this tool to:

    - Detect deployment lag: compare ``server_version`` against the
      release you expect before relying on a recently-shipped fix.
    - Understand who ``"me"`` resolves to: ``current_user`` shows the
      identity this call runs as -- the configured API key in ``legacy``
      mode, the caller's own in the per-user modes. When a shared or robot
      API key is in use, ``assigned_to_id="me"`` will match **that** account,
      not the human operator — call this tool first if issue queries
      assigned to "me" return unexpectedly empty results.
    - Check which plugin-gated tool families are active.

    Returns:
        A dict with:

        - ``server_version`` (str): the deployed package version (from
          ``importlib.metadata``). The literal ``"0.0.0+unknown"`` when
          the package metadata is unavailable (rare; source-tree runs
          without an editable install).
        - ``read_only_mode`` (bool): whether ``REDMINE_MCP_READ_ONLY``
          is enabled. When ``True``, all write tools refuse with the
          standard read-only error.
        - ``auth_mode`` (str): the configured ``REDMINE_AUTH_MODE``, e.g.
          ``"legacy"``, ``"legacy-per-user"``, ``"oauth"``,
          ``"oauth-proxy"`` or ``"api-key-login"``.
        - ``current_user`` (dict | None): ``{id, login, name}`` for the
          authenticated Redmine user. ``None`` if the server cannot reach
          Redmine (check ``/health`` for connectivity status).
        - ``plugin_flags`` (dict[str, bool]): which plugin-gated tool
          families are enabled. Always ``agile``, ``checklists``,
          ``products``, ``crm``, ``deals``, ``dmsf``, ``helpdesk``,
          ``helpdesk_tickets``, ``tags``, in that order, and after them
          one key per family this deployment added through
          ``REDMINE_MCP_EXTENSIONS``, named after the family. The nine are
          reserved, so each always carries its own flag.
          ``True`` means the family's tools are listed and routable;
          ``False`` means they are hidden from ``tools/list`` (``agile``
          and ``tags`` only add fields to core tools and are never
          hidden).

    The response intentionally excludes credentials, internal
    hostnames, file-system paths, and any other operator-config that
    a caller doesn't need to know to choose its call shape.

    Example:
        >>> await get_mcp_server_info()
        {
            "server_version": "1.3.0",
            "read_only_mode": False,
            "auth_mode": "legacy",
            "current_user": {"id": 5, "login": "vitex", "name": "Vítězslav Dvořák"},
            "plugin_flags": {
                "agile": False,
                "checklists": False,
                "products": False,
                "crm": False,
                "dmsf": True,
                "tags": False,
            },
        }
    """
    import os

    return {
        "server_version": __version__,
        "read_only_mode": _is_read_only_mode(),
        "auth_mode": (os.environ.get("REDMINE_AUTH_MODE") or "legacy").lower(),
        "current_user": await _fetch_current_user_info(),
        "plugin_flags": {
            **{
                family: is_enabled()
                for family, is_enabled in SERVER_INFO_PLUGIN_FLAGS.items()
            },
            **{spec.family: spec.enabled() for spec in REGISTERED_EXTENSIONS},
        },
    }
