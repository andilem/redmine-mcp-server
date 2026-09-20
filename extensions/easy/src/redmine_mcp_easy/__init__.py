"""Easy Redmine support for redmine-mcp-server, as an out-of-tree extension.

Easy Redmine is a commercial fork rather than a plugin: it serves the same
``/issues.json`` with extra attributes, registers query filters stock Redmine
does not have, and adds entities -- sprints, attendances, checklists -- that
``python-redmine`` has no resource class for. None of that belongs in the
upstream server, and carrying it as a patch on upstream's own files meant a
conflict in ``tools/issues.py`` on every release.

So it lives here instead, and touches nothing upstream owns. What used to be
a diff is now three declarations:

- ``issue_update_keys`` for the attributes ``update_redmine_issue`` and
  ``create_redmine_issue`` may write. They have to be *registered* rather
  than left to the custom-field path: ``_normalize_field_label`` strips
  non-alphanumerics, so a custom field named "Easy Sprint ID" normalizes to
  the same ``easysprintid`` as ``easy_sprint_id`` and would silently swallow
  the value.
- ``issue_query_filters`` for ``easy_sprint_id``, with the companion
  parameter that makes it count. Easy replaces Redmine's filter handling
  with EasyQuery, which engages on ``set_filter``; without it the filter is
  dropped and Redmine answers 200 with the collection unnarrowed, which
  reads exactly like a filter that matched everything.
- ``tool_kinds`` and ``tool_scopes`` for the nine tools below.

The issue *response* needs almost none. Easy's top-level keys
(``easy_sprint``, ``easy_story_points`` and the two sprint-position ones)
come back under ``unmapped_fields``, which upstream's serializer emits for
exactly this case -- a distribution's own keys, read from the payload rather
than fetched, and wrapped against prompt injection like every other
user-authored string. The shape is Redmine's own rather than one this
package chose, which is the price of not being a patch.

The one exception is ``issue_payload_skip_keys``: ``unmapped_fields`` would
otherwise carry Easy's ``css_classes`` on every single issue, and that one
is for its renderer and nobody else.

Deployment::

    REDMINE_MCP_EXTENSIONS=redmine_mcp_easy
    REDMINE_EASY_ENABLED=true
    REDMINE_EASY_DB_URL=mysql://user:pass@host:3306/easyredmine   # sprints only

The import order below is not a style preference. ``@mcp.tool()`` reads a
tool's annotations out of ``TOOL_KINDS`` at decoration time, so the spec has
to be registered before the modules that define the tools are imported.
"""

from redmine_mcp_server.extensions import (
    ExtensionSpec,
    ToolKind,
    is_true_env,
    register_extension,
)

__version__ = "1.0.0"

# The server version this was written against. `extensions.py` calls itself
# provisional and may change the shape of ExtensionSpec in a minor release,
# so the dependency in pyproject.toml pins a range rather than a floor.
REQUIRES_SERVER = "~=2.17"


def is_enabled() -> bool:
    """Whether this family's tools are listed. Read per call, like the rest."""
    return is_true_env("REDMINE_EASY_ENABLED", "false")


register_extension(
    ExtensionSpec(
        family="easy",
        enabled=is_enabled,
        tool_kinds={
            "list_easy_sprints": ToolKind.READ,
            "list_easy_attendances": ToolKind.READ,
            # create/update: update overwrites what is there, like
            # manage_redmine_news and manage_time_entry.
            "manage_easy_attendance": ToolKind.WRITE_DESTRUCTIVE,
            "delete_easy_attendance": ToolKind.WRITE_DESTRUCTIVE_IDEMPOTENT,
            # Not additive: approving signs off someone's working time, and
            # the decision overwrites whatever approval_status was there.
            "approve_easy_attendances": ToolKind.WRITE_DESTRUCTIVE,
            "list_easy_checklists": ToolKind.READ,
            # Both manage tools update as well as create.
            "manage_easy_checklist": ToolKind.WRITE_DESTRUCTIVE,
            "manage_easy_checklist_item": ToolKind.WRITE_DESTRUCTIVE,
            "delete_easy_checklist": ToolKind.WRITE_DESTRUCTIVE_IDEMPOTENT,
        },
        # Easy's plugins ship no public permission list, so there is no name
        # to map these onto: a guessed one would either deny every call or
        # gate on something that does not exist. They are mapped as
        # scope-free, which means token scopes do not narrow them and
        # Redmine's own server-side permissions are what stands. Leaving
        # them out instead would be worse -- the middleware denies an
        # unmapped tool outright, so these would be dead under enforcement
        # rather than merely ungated.
        tool_scopes={
            "list_easy_sprints": frozenset(),
            "list_easy_attendances": frozenset(),
            "manage_easy_attendance": frozenset(),
            "delete_easy_attendance": frozenset(),
            "approve_easy_attendances": frozenset(),
            "list_easy_checklists": frozenset(),
            "manage_easy_checklist": frozenset(),
            "manage_easy_checklist_item": frozenset(),
            "delete_easy_checklist": frozenset(),
        },
        # Easy Redmine's writable issue attributes, from IssueApiRequest in
        # the instance's own /easy_swagger.json.
        issue_update_keys=("easy_sprint_id", "easy_story_points", "target_backlog"),
        # Only filters verified against a live instance belong here: an
        # unregistered name is dropped by Redmine, which then answers 200
        # with the collection unnarrowed.
        issue_query_filters={"easy_sprint_id": {"set_filter": 1}},
        # `css_classes` is the CSS class list Easy's issue grid renders
        # with. It rides on every issue -- 99 to 144 characters across a
        # page of 25, 202 on the wire once wrapped, 5,062 for a default
        # listing -- and answers nothing the issue's own fields do not
        # answer better: `status-11` beside `status`, `overdue` beside
        # `due_date`. Too short for the size cap to catch, which is why
        # #331 exists.
        #
        # `is_favorited` deliberately stays. It is per-user state, not
        # presentation, and a caller can reasonably want it.
        issue_payload_skip_keys=("css_classes",),
    )
)

from . import attendances  # noqa: E402,F401  -- registers its tools
from . import checklists  # noqa: E402,F401  -- registers its tools
from . import sprints  # noqa: E402,F401  -- registers its tools
