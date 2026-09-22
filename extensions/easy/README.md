# redmine-mcp-easy

Easy Redmine support for [redmine-mcp-server](https://github.com/jztan/redmine-mcp-server),
as an out-of-tree extension.

Easy Redmine is a commercial fork rather than a plugin. It serves the same
`/issues.json` with extra attributes, registers query filters stock Redmine
does not have, and adds entities — sprints, attendances, checklists — that
`python-redmine` has no resource class for. None of that belongs upstream,
and carrying it as a patch meant a conflict in `tools/issues.py` on every
release. This package touches nothing upstream owns.

## Install

```bash
pip install -e extensions/easy        # the nine tools over the REST API
pip install -e "extensions/easy[mysql]"   # plus list_easy_sprints
```

The `mysql` extra is only for `list_easy_sprints`: Easy Redmine serves no
sprint endpoint — `/easy_sprints.json` answers 403 even with a valid API key
— so sprint names are read from the database. The other eight tools need
nothing beyond the server.

## Configure

```bash
REDMINE_MCP_EXTENSIONS=redmine_mcp_easy   # the server imports this at startup
REDMINE_EASY_ENABLED=true                 # makes the family's tools visible
REDMINE_EASY_DB_URL=mysql://user:pass@host:3306/easyredmine   # sprints only
```

The two flags do different things. `REDMINE_MCP_EXTENSIONS` decides whether
this package is loaded at all; `REDMINE_EASY_ENABLED` decides whether its
tools are listed and whether its issue keys and query filters count. Without
the DSN the server starts normally and only `list_easy_sprints` answers
`EASY_DB_NOT_CONFIGURED` — setting a sprint by id works without a database.

## Tools

| Tool | |
|---|---|
| `list_easy_sprints` | needs `REDMINE_EASY_DB_URL` |
| `list_easy_attendances`, `manage_easy_attendance`, `delete_easy_attendance`, `approve_easy_attendances` | REST |
| `list_easy_checklists`, `manage_easy_checklist`, `manage_easy_checklist_item`, `delete_easy_checklist` | REST |

They are mapped as scope-free: Easy's plugins ship no public permission
list, so there is no name to gate on, and Redmine's own server-side
permissions are what stands. Leaving them unmapped instead would be worse —
the scope middleware denies an unmapped tool outright.

Every checklist write ends by reading the checklist back, because the write
endpoints answer either with nothing or about a single entry. That read can
fail where the write succeeded, so it is never reported as a failed write:
the result says `created`/`updated` and carries a note instead of the
contents. Do not retry on that note — a repeated create is a second
checklist, not a no-op.

## Issues

`easy_sprint_id`, `easy_story_points` and `target_backlog` are registered as
issue update keys, so `create_redmine_issue` and `update_redmine_issue` write
them. They have to be registered rather than left to the custom-field path:
`_normalize_field_label` strips non-alphanumerics, so a custom field named
"Easy Sprint ID" normalizes to the same `easysprintid` as `easy_sprint_id`
and would silently swallow the value.

`easy_sprint_id` is registered as a query filter with `set_filter=1`
alongside it. Easy replaces Redmine's filter handling with EasyQuery, which
engages on that parameter; without it the filter is dropped and Redmine
answers 200 with the collection unnarrowed, which reads exactly like a
filter that matched everything.

Reading needs almost no registration. Easy's top-level issue keys come back
under `unmapped_fields`, which upstream's serializer emits for exactly this
case. The shape is Redmine's own — `unmapped_fields.easy_sprint` rather than
a top-level `easy_sprint` normalized to `{id, name, due_date}` — which is
the price of not being a patch.

The exception is `css_classes`, declared in `issue_payload_skip_keys` and
therefore absent. It is the CSS class list Easy's issue grid renders with:
99–144 characters on every issue, 202 on the wire once wrapped, 5,062 for a
default 25-issue listing, and it answers nothing the issue's own fields do
not answer better — `status-11` beside `status`, `overdue` beside
`due_date`. `is_favorited` stays: that is per-user state, not presentation.

## Database access

`_db.py` is the one exception to "everything goes through the REST API", and
it is kept as narrow as an exception should be: named queries only with no
SQL entry point, the session set `READ ONLY` on connect, explicit timeouts,
and every row re-checked for visibility against the *caller's* API key in
`sprints.py` — a database read bypasses Redmine's authorization completely.

## Tests

```bash
pytest extensions/easy/tests
```

Run them on their own, not in the same session as the server's own suite.
Two of them import `redmine_mcp_server.main`, which loads extensions,
applies plugin visibility and installs middleware once per process -- they
do that in a subprocess for exactly that reason, but the server's suite
patches enough module state that a combined run is not a thing either
suite is written for.
