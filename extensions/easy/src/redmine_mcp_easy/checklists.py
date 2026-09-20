"""Easy Redmine checklists: the to-do lists that hang off an issue.

Registered only when ``REDMINE_EASY_ENABLED=true``. These are Easy's own
``easy_checklists``, not the RedmineUP plugin behind ``get_checklist`` and
``create_checklist_item``: different endpoints, different field names. The
``easy_`` prefix keeps the two apart, because mixing them up produces calls
that look right and fail.

The API has one gap that shapes every tool here: **there is no index**.
``/easy_checklists.json`` accepts a POST and nothing else, and checklist
items have no read endpoint at all. Checklists are read through the issue
they belong to, with ``include=checklists``, which is also the only way to
learn the ids the write calls need. So the read tool asks Redmine for an
issue even though it answers with checklists.

Two more consequences of that shape:

- **Writes are verified by reading back.** A create answers 201 with the
  record, but an update answers 200 with nothing promised, and an item
  write answers about the item rather than the list it sits in. Every write
  here reports a fresh read of the checklist, so what the caller sees is
  the state that exists, not the request that was sent.
- **Nested items are a Rails ``accepts_nested_attributes_for``.** An entry
  with an ``id`` updates that item, one without adds it. Whether ``_destroy``
  also removes one is undocumented, so deleting stays with the endpoint that
  is documented for it, and this module never sends ``_destroy``.
"""

import logging
from typing import Any, Dict, List, Optional, Union

from redminelib.exceptions import ForbiddenError
from redmine_mcp_server.extensions import (
    ActionMode,
    READ_ONLY_ERROR,
    action_dispatch,
    handle_redmine_error,
    in_thread,
    is_positive_int,
    is_read_only_mode,
    mcp,
    offloaded,
    plugin_tag,
    wrap_insecure_content,
)
from ._api import as_dict, describe, easy_request

logger = logging.getLogger(__name__)

# Easy's issue payload names the collection "checklists" in the include
# parameter. The key it answers with is not documented, and the schemas use
# "easy_checklists" elsewhere, so both are accepted rather than betting on
# one and reporting an empty list when the bet is wrong.
_CHECKLIST_KEYS = ("checklists", "easy_checklists")

# entity_type is fixed rather than exposed. The response carries a generic
# "entity" and other schemas mention easy_checklists too, so Easy may well
# attach them to more than issues -- but an unverified parameter that
# silently does nothing is worse than one that is not offered.
_ENTITY_TYPE = "Issue"

_MAX_ITEMS = 100

# Checklists are a project module. Redmine checks a module before it checks
# any permission, so a project without this one refuses an administrator
# too, and the bare 403 reads as a missing right.
_CHECKLIST_MODULE = "easy_checklists"


def _module_missing_on(issue_id: Optional[int]) -> bool:
    """Whether the issue's project has the checklist module switched off.

    Answers ``False`` when that cannot be established -- an unreadable
    issue, an unreadable project, an error on the way -- because claiming a
    disabled module sends an operator to switch on something that may
    already be on.
    """
    if not is_positive_int(issue_id):
        return False
    try:
        payload = easy_request("get", f"issues/{issue_id}.json")
        project_id = as_dict(as_dict(as_dict(payload).get("issue")).get("project")).get(
            "id"
        )
        if project_id is None:
            return False
        project = easy_request(
            "get", f"projects/{project_id}.json", params={"include": "enabled_modules"}
        )
    except Exception as exc:  # noqa: BLE001 -- diagnosis, never the answer
        logger.debug("Could not check the checklist module: %s", exc)
        return False
    modules = as_dict(as_dict(project).get("project")).get("enabled_modules")
    if not isinstance(modules, list):
        return False
    names = {as_dict(m).get("name") for m in modules}
    return _CHECKLIST_MODULE not in names


def _module_disabled_error(issue_id: int) -> Dict[str, Any]:
    return {
        "error": (
            "Redmine refused the request because the issue's project has "
            "the checklist module switched off."
        ),
        "hint": (
            "Enable it under Project settings > Modules > Checklists. "
            "Redmine checks the module before permissions, so this is "
            "refused even for an administrator. get_project_modules shows "
            "what a project has enabled."
        ),
        "code": "CHECKLIST_MODULE_DISABLED",
        "upstream_status": 403,
        "issue_id": issue_id,
    }


def _item_to_dict(row: Any) -> Dict[str, Any]:
    """Project one ``easy_checklist_item``."""
    row = as_dict(row)
    return {
        "id": row.get("id"),
        # What someone typed into a to-do line. Wrapped like every other
        # user-authored string that reaches a model.
        "subject": wrap_insecure_content(row.get("subject") or ""),
        "done": row.get("done"),
        "position": row.get("position"),
        "author": _ref(row.get("author")),
        "changed_by": _ref(row.get("changed_by")),
        "updated_at": row.get("updated_at"),
    }


def _checklist_to_dict(row: Any) -> Dict[str, Any]:
    """Project one ``easy_checklist`` with its items and a done count."""
    row = as_dict(row)
    raw_items = row.get("easy_checklist_items")
    items = [_item_to_dict(i) for i in raw_items] if isinstance(raw_items, list) else []
    return {
        "id": row.get("id"),
        "name": wrap_insecure_content(row.get("name") or ""),
        "entity": _ref(row.get("entity")),
        "author": _ref(row.get("author")),
        "items": items,
        # The question a caller actually has about a checklist.
        "done_count": sum(1 for i in items if i.get("done")),
        "item_count": len(items),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _ref(value: Any) -> Optional[Dict[str, Any]]:
    """``{id, name}`` for an Easy association, or ``None``.

    An id-less object counts as absent. Easy sends ``entity`` as a present
    but empty object on a checklist read, and reporting ``{"id": null,
    "name": null}`` would be noise in every response.
    """
    ref = as_dict(value)
    if not ref or ref.get("id") is None:
        return None
    return {"id": ref.get("id"), "name": ref.get("name")}


def _checklists_of_issue(payload: Any) -> List[Any]:
    """The checklist array inside an issue payload, whichever key holds it."""
    issue = as_dict(as_dict(payload).get("issue"))
    for key in _CHECKLIST_KEYS:
        rows = issue.get(key)
        if isinstance(rows, list):
            return rows
    return []


def _read_checklist(checklist_id: int) -> Optional[Dict[str, Any]]:
    """One checklist as the server has it now, or ``None``."""
    payload = easy_request("get", f"easy_checklists/{checklist_id}.json")
    record = as_dict(payload).get("easy_checklist")
    return _checklist_to_dict(record) if as_dict(record) else None


def _normalize_items(items: Any) -> Union[List[Dict[str, Any]], str]:
    """Turn the ``items`` argument into Easy's nested attributes.

    Accepts the two shapes a caller reasonably passes: bare strings, which
    become unticked entries, and dicts with ``subject``, ``done``, ``id``
    and ``position``. An ``id`` updates that item, its absence adds one --
    that is Rails' nested-attributes rule, not a choice made here.

    Returns the list, or a string describing what was wrong with it.
    """
    if not isinstance(items, list):
        return "items must be a list."
    if len(items) > _MAX_ITEMS:
        return f"At most {_MAX_ITEMS} items per call."

    out: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            if not item.strip():
                return f"items[{index}] is empty."
            out.append({"subject": item, "done": False})
            continue
        if not isinstance(item, dict):
            return f"items[{index}] must be a string or an object."

        entry: Dict[str, Any] = {}
        subject = item.get("subject")
        if subject is not None:
            if not str(subject).strip():
                return f"items[{index}].subject is empty."
            entry["subject"] = subject
        if "done" in item:
            entry["done"] = bool(item["done"])
        if item.get("id") is not None:
            if not is_positive_int(item["id"]):
                return f"items[{index}].id must be a positive integer."
            entry["id"] = item["id"]
        if item.get("position") is not None:
            entry["new_position"] = item["position"]
        if "subject" not in entry and "id" not in entry:
            return f"items[{index}] needs a subject, or an id to change."
        out.append(entry)
    return out


async def list_easy_checklists(
    issue_id: Optional[int] = None,
    checklist_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Read the checklists on an issue, or one checklist by id.

    Easy serves no checklist index, so an issue's checklists are read from
    the issue itself (``GET /issues/{id}.json?include=checklists``). This is
    also where the ids come from: every other checklist tool needs one, and
    nothing else reports them.

    Args:
        issue_id: Issue whose checklists to read. Exactly one of this and
            ``checklist_id`` is required.
        checklist_id: A single checklist, read directly.

    Returns:
        ``{"checklists": [...]}``, each as ``{id, name, entity, author,
        items, done_count, item_count, created_at, updated_at}`` with items
        as ``{id, subject, done, position, author, changed_by,
        updated_at}``. An issue without checklists answers with an empty
        list. On failure, a dict with an ``"error"`` key.

    Note:
        These are Easy Redmine's checklists. The RedmineUP plugin's are a
        different feature behind ``get_checklist``.
    """
    if (issue_id is None) == (checklist_id is None):
        return {"error": "Pass exactly one of issue_id and checklist_id."}
    if issue_id is not None and not is_positive_int(issue_id):
        return {"error": "issue_id must be a positive integer."}
    if checklist_id is not None and not is_positive_int(checklist_id):
        return {"error": "checklist_id must be a positive integer."}

    def _run() -> Dict[str, Any]:
        if checklist_id is not None:
            try:
                checklist = _read_checklist(checklist_id)
            except Exception as exc:
                return handle_redmine_error(exc, "reading an Easy Redmine checklist")
            return {"checklists": [checklist] if checklist else []}

        try:
            payload = easy_request(
                "get", f"issues/{issue_id}.json", params={"include": "checklists"}
            )
        except Exception as exc:
            return handle_redmine_error(
                exc, "reading an issue's Easy Redmine checklists"
            )
        return {
            "checklists": [
                _checklist_to_dict(c) for c in _checklists_of_issue(payload)
            ],
            "issue_id": issue_id,
        }

    return await in_thread(_run)


async def _create_checklist_action(
    issue_id: Optional[int] = None,
    name: Optional[str] = None,
    items: Optional[List[Any]] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """POST /easy_checklists.json, which answers 201 with the record."""
    if not is_positive_int(issue_id):
        return {"error": "create needs issue_id, a positive integer."}
    if not str(name or "").strip():
        return {"error": "create needs a name."}

    body: Dict[str, Any] = {
        "name": name,
        "entity_id": issue_id,
        "entity_type": _ENTITY_TYPE,
    }
    if items is not None:
        normalized = _normalize_items(items)
        if isinstance(normalized, str):
            return {"error": normalized}
        body["easy_checklist_items_attributes"] = normalized

    def _run() -> Dict[str, Any]:
        try:
            payload = easy_request(
                "post", "easy_checklists.json", data={"easy_checklist": body}
            )
        except ForbiddenError as exc:
            # Only create knows an issue, and an issue is the only way to
            # reach a project: a checklist read reports its entity as an
            # empty object, so update and delete cannot name this cause and
            # do not try.
            if _module_missing_on(issue_id):
                return _module_disabled_error(issue_id)
            return handle_redmine_error(exc, "creating an Easy Redmine checklist")
        except Exception as exc:
            return handle_redmine_error(exc, "creating an Easy Redmine checklist")
        record = as_dict(payload).get("easy_checklist")
        if not as_dict(record).get("id"):
            return {
                "error": (
                    "The server accepted the checklist but did not return "
                    "it, so it cannot be confirmed."
                ),
                "code": "CREATE_UNCONFIRMED",
                "hint": (
                    "Check with list_easy_checklists before creating it "
                    "again. Response was: " + describe(payload)
                ),
            }
        return {"checklist": _checklist_to_dict(record), "created": True}

    return await in_thread(_run)


async def _update_checklist_action(
    checklist_id: Optional[int] = None,
    name: Optional[str] = None,
    items: Optional[List[Any]] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """PUT /easy_checklists/{id}.json, then read the checklist back."""
    if not is_positive_int(checklist_id):
        return {"error": "update needs checklist_id, a positive integer."}

    body: Dict[str, Any] = {}
    if name is not None:
        if not str(name).strip():
            return {"error": "name cannot be blank."}
        body["name"] = name
    if items is not None:
        normalized = _normalize_items(items)
        if isinstance(normalized, str):
            return {"error": normalized}
        body["easy_checklist_items_attributes"] = normalized
    if not body:
        return {
            "error": "update needs a name or items to change.",
            "hint": (
                "An item with an id changes that item; one without adds it. "
                "Deleting is delete_easy_checklist."
            ),
        }

    def _run() -> Dict[str, Any]:
        try:
            easy_request(
                "put",
                f"easy_checklists/{checklist_id}.json",
                data={"easy_checklist": body},
            )
        except Exception as exc:
            return handle_redmine_error(exc, "updating an Easy Redmine checklist")
        try:
            checklist = _read_checklist(checklist_id)
        except Exception as exc:
            return handle_redmine_error(
                exc, "reading back the updated Easy Redmine checklist"
            )
        return {"checklist": checklist, "updated": True}

    return await in_thread(_run)


@action_dispatch(
    {
        "create": ActionMode.WRITE,
        "update": ActionMode.WRITE,
    }
)
async def manage_easy_checklist(
    action: str,
    issue_id: Optional[int] = None,
    checklist_id: Optional[int] = None,
    name: Optional[str] = None,
    items: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Create or rename an Easy Redmine checklist on an issue.

    A checklist and its entries can be created in one call, because Easy
    accepts the items as nested attributes. Single entries are
    ``manage_easy_checklist_item``, which is also where ticking one off
    belongs; deleting is ``delete_easy_checklist``.

    Args:
        action: One of ``create``, ``update``.
        issue_id: Issue to attach the list to. Required for ``create``.
        checklist_id: List to change. Required for ``update``.
        name: The list's name. Required for ``create``, optional for
            ``update`` (cannot be blank).
        items: Entries, as strings (``"Ticket schließen"``, added unticked)
            or objects with ``subject``, ``done``, ``position`` and, to
            change an existing entry rather than add one, ``id``. At most
            100 per call.

    Returns:
        ``{"checklist": {...}, "created"|"updated": True}``, the checklist
        as the server has it afterwards. A create the server did not
        confirm reports ``code: CREATE_UNCONFIRMED`` instead of a
        made-up record. On error, ``{"error": "..."}``.

        Blocked in read-only mode (``REDMINE_MCP_READ_ONLY=true``).

    Note:
        Checklists attach to issues here. Easy's payload suggests other
        entity types exist, but nothing documents them, so the type is
        fixed rather than offered as a parameter that may do nothing.
    """
    return {
        "create": _create_checklist_action,
        "update": _update_checklist_action,
    }


async def _create_item_action(
    checklist_id: Optional[int] = None,
    subject: Optional[str] = None,
    done: Optional[bool] = None,
    position: Optional[int] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """POST /easy_checklist_items.json."""
    if not is_positive_int(checklist_id):
        return {"error": "create needs checklist_id, a positive integer."}
    if not str(subject or "").strip():
        return {"error": "create needs a subject."}

    body: Dict[str, Any] = {
        "easy_checklist_id": checklist_id,
        "subject": subject,
        "done": bool(done),
    }
    if position is not None:
        body["new_position"] = position

    def _run() -> Dict[str, Any]:
        try:
            easy_request(
                "post",
                "easy_checklist_items.json",
                data={"easy_checklist_item": body},
            )
        except Exception as exc:
            return handle_redmine_error(exc, "creating an Easy Redmine checklist item")
        return _with_checklist(checklist_id, "created")

    return await in_thread(_run)


async def _update_item_action(
    item_id: Optional[int] = None,
    checklist_id: Optional[int] = None,
    subject: Optional[str] = None,
    done: Optional[bool] = None,
    position: Optional[int] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """PUT /easy_checklist_items/{id}.json."""
    if not is_positive_int(item_id):
        return {"error": "update needs item_id, a positive integer."}

    body: Dict[str, Any] = {}
    if subject is not None:
        if not str(subject).strip():
            return {"error": "subject cannot be blank."}
        body["subject"] = subject
    if done is not None:
        body["done"] = bool(done)
    if position is not None:
        body["new_position"] = position
    if not body:
        return {"error": "update needs subject, done or position."}

    def _run() -> Dict[str, Any]:
        try:
            payload = easy_request(
                "put",
                f"easy_checklist_items/{item_id}.json",
                data={"easy_checklist_item": body},
            )
        except Exception as exc:
            return handle_redmine_error(exc, "updating an Easy Redmine checklist item")
        # The item's own answer names its checklist, which is how the whole
        # list can be reported back without the caller having to say where
        # the item lives.
        owner = checklist_id or as_dict(
            as_dict(as_dict(payload).get("easy_checklist_item")).get("easy_checklist")
        ).get("id")
        return _with_checklist(owner, "updated")

    return await in_thread(_run)


def _with_checklist(checklist_id: Optional[int], verb: str) -> Dict[str, Any]:
    """Report the owning checklist after an item write, if it can be read.

    The item endpoints answer about the item, which leaves the caller
    without the thing it asked about -- how the list looks now. A failed
    read-back is reported as done-but-unread rather than as an error: the
    write already happened.
    """
    result: Dict[str, Any] = {verb: True}
    if not is_positive_int(checklist_id):
        return result
    try:
        checklist = _read_checklist(checklist_id)
    except Exception as exc:  # noqa: BLE001 -- the write already happened
        logger.debug("Checklist %s not readable after a write: %s", checklist_id, exc)
        result["note"] = (
            f"The item was {verb}, but checklist {checklist_id} could not be "
            "read back."
        )
        return result
    if checklist:
        result["checklist"] = checklist
    return result


@action_dispatch(
    {
        "create": ActionMode.WRITE,
        "update": ActionMode.WRITE,
    }
)
async def manage_easy_checklist_item(
    action: str,
    checklist_id: Optional[int] = None,
    item_id: Optional[int] = None,
    subject: Optional[str] = None,
    done: Optional[bool] = None,
    position: Optional[int] = None,
) -> Dict[str, Any]:
    """Add an entry to a checklist, tick one off, rename or move it.

    This is the tool for the common case. Working through
    ``manage_easy_checklist`` would mean sending a nested array to change
    one boolean.

    Args:
        action: One of ``create``, ``update``.
        checklist_id: List to add to. Required for ``create``; on
            ``update`` it only saves a lookup, since the server names the
            entry's list itself.
        item_id: Entry to change. Required for ``update``.
        subject: The entry's text. Required for ``create``, optional for
            ``update`` (cannot be blank).
        done: Ticked or not. Defaults to ``False`` on ``create``.
        position: 1-based position in the list.

    Returns:
        ``{"created"|"updated": True, "checklist": {...}}`` -- the whole
        list as it stands afterwards, because that is what a caller wants
        to see, not the single entry. If the list cannot be read back, the
        write is still reported, with a ``note`` saying so. On error,
        ``{"error": "..."}``.

        Blocked in read-only mode (``REDMINE_MCP_READ_ONLY=true``).
    """
    return {
        "create": _create_item_action,
        "update": _update_item_action,
    }


@offloaded
def delete_easy_checklist(
    checklist_id: Optional[int] = None,
    item_id: Optional[int] = None,
    confirm_delete: bool = False,
) -> Dict[str, Any]:
    """Delete a whole checklist, or a single entry from one.

    Deletion is **irreversible**, and deleting a list takes its entries
    with it. Like the other destructive tools here, this one refuses
    unless ``confirm_delete=True``, and the refusal carries what would be
    lost -- for a list, that includes how many entries are on it.

    Args:
        checklist_id: The list to delete, with everything on it.
        item_id: A single entry to delete. Pass exactly one of the two.
        confirm_delete: When ``False`` (default), the tool refuses and
            returns the preview. Pass ``True`` to actually delete.

    Returns:
        On refusal: ``{"error", "code": "CONFIRMATION_REQUIRED",
        "checklist": {...}}``. On success: ``{"deleted": True}`` with the
        id that went, plus the remaining list when an entry was deleted.
        On error, ``{"error": "..."}``.

        Blocked in read-only mode (``REDMINE_MCP_READ_ONLY=true``).

    Note:
        An entry has no read endpoint of its own, so a refusal for
        ``item_id`` cannot preview the entry's text. Read the list with
        ``list_easy_checklists`` first if that matters.
    """
    if is_read_only_mode():
        return dict(READ_ONLY_ERROR)
    if (checklist_id is None) == (item_id is None):
        return {"error": "Pass exactly one of checklist_id and item_id."}

    if checklist_id is not None:
        if not is_positive_int(checklist_id):
            return {"error": "checklist_id must be a positive integer."}
        try:
            checklist = _read_checklist(checklist_id)
        except Exception as exc:
            return handle_redmine_error(exc, "reading the Easy Redmine checklist")
        if checklist is None:
            return {
                "error": f"Checklist {checklist_id} does not exist.",
                "code": "NOT_FOUND",
            }
        if not confirm_delete:
            return {
                "error": (
                    f"Deleting checklist {checklist_id} removes it and its "
                    f"{checklist['item_count']} entr"
                    f"{'y' if checklist['item_count'] == 1 else 'ies'}. "
                    "This is irreversible. Pass confirm_delete=True to "
                    "proceed."
                ),
                "code": "CONFIRMATION_REQUIRED",
                "checklist": checklist,
            }
        try:
            easy_request("delete", f"easy_checklists/{checklist_id}.json")
        except Exception as exc:
            return handle_redmine_error(exc, "deleting the Easy Redmine checklist")
        return {"deleted": True, "checklist_id": checklist_id, "checklist": checklist}

    if not is_positive_int(item_id):
        return {"error": "item_id must be a positive integer."}
    if not confirm_delete:
        return {
            "error": (
                f"Deleting entry {item_id} is irreversible. Pass "
                "confirm_delete=True to proceed."
            ),
            "code": "CONFIRMATION_REQUIRED",
            "hint": (
                "Easy serves no read endpoint for a single entry, so this "
                "refusal cannot show its text. list_easy_checklists does."
            ),
        }
    try:
        easy_request("delete", f"easy_checklist_items/{item_id}.json")
    except Exception as exc:
        return handle_redmine_error(exc, "deleting the Easy Redmine checklist item")
    return {"deleted": True, "item_id": item_id}


# Registered unconditionally and tagged: the family's `enabled`
# callable in the ExtensionSpec is what hides these when
# REDMINE_EASY_ENABLED is off. Decorating at the bottom rather than
# at each `def` keeps the tool's own signature readable, and the
# annotations are read out of TOOL_KINDS here either way.
list_easy_checklists = mcp.tool(tags={plugin_tag("easy")})(list_easy_checklists)
manage_easy_checklist = mcp.tool(tags={plugin_tag("easy")})(manage_easy_checklist)
manage_easy_checklist_item = mcp.tool(tags={plugin_tag("easy")})(
    manage_easy_checklist_item
)
delete_easy_checklist = mcp.tool(tags={plugin_tag("easy")})(delete_easy_checklist)
