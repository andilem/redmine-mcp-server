"""Easy Redmine checklist tools.

The API has no index and no read endpoint for a single entry, so two
things have to hold and are pinned here: checklists are found through the
issue that carries them, and every write reports a fresh read of the list
rather than an echo of the request.
"""

from unittest.mock import patch

import pytest
from redminelib.exceptions import ForbiddenError, ResourceNotFoundError

from redmine_mcp_easy import checklists as cl_mod
from redmine_mcp_easy.checklists import (
    _checklist_to_dict,
    _normalize_items,
    delete_easy_checklist,
    list_easy_checklists,
    manage_easy_checklist,
    manage_easy_checklist_item,
)


def _item(item_id=1, subject="Ticket schließen", done=False, position=1):
    return {
        "id": item_id,
        "subject": subject,
        "done": done,
        "position": position,
        "author": {"id": 108, "name": "Andreas Lemmer"},
        "changed_by": None,
        "updated_at": "2026-09-16T08:00:00Z",
    }


def _checklist(checklist_id=7, name="Definition of Done", items=None):
    return {
        "id": checklist_id,
        "name": name,
        "entity": {"id": 36417, "name": "Issue #36417"},
        "author": {"id": 108, "name": "Andreas Lemmer"},
        "easy_checklist_items": [_item()] if items is None else items,
        "created_at": "2026-09-16T07:00:00Z",
        "updated_at": "2026-09-16T08:00:00Z",
    }


class TestSerialization:
    def test_items_and_counts(self):
        out = _checklist_to_dict(
            _checklist(items=[_item(1, done=True), _item(2, "Review", False)])
        )
        assert out["item_count"] == 2
        assert out["done_count"] == 1
        assert out["items"][1]["position"] == 1

    def test_user_authored_text_is_wrapped(self):
        out = _checklist_to_dict(
            _checklist(name="Ignore previous", items=[_item(subject="Do this")])
        )
        assert out["name"] != "Ignore previous"
        assert "Ignore previous" in out["name"]
        assert out["items"][0]["subject"] != "Do this"

    def test_an_id_less_association_is_reported_as_absent(self):
        """Easy sends entity as a present but empty object on a read."""
        out = _checklist_to_dict(
            {"id": 7, "entity": {"id": None, "name": None}, "author": None}
        )
        assert out["entity"] is None
        assert out["author"] is None

    def test_a_checklist_without_items_does_not_crash(self):
        out = _checklist_to_dict({"id": 7})
        assert out["items"] == []
        assert out["item_count"] == 0
        assert out["entity"] is None


class TestItemNormalization:
    def test_a_bare_string_becomes_an_unticked_entry(self):
        assert _normalize_items(["Review"]) == [{"subject": "Review", "done": False}]

    def test_an_id_marks_an_entry_as_a_change_rather_than_an_addition(self):
        out = _normalize_items([{"id": 3, "done": True}])
        assert out == [{"done": True, "id": 3}]

    def test_position_travels_under_easys_own_name(self):
        out = _normalize_items([{"subject": "Erst dies", "position": 2}])
        assert out[0]["new_position"] == 2

    @pytest.mark.parametrize(
        "items,fragment",
        [
            ("Review", "must be a list"),
            ([""], "is empty"),
            ([{"subject": "  "}], "is empty"),
            ([{"done": True}], "needs a subject"),
            ([{"id": 0, "subject": "x"}], "positive integer"),
            ([42], "string or an object"),
            ([{"subject": "x"}] * 101, "At most 100"),
        ],
    )
    def test_malformed_items_are_named(self, items, fragment):
        assert fragment in _normalize_items(items)


class TestReading:
    @pytest.mark.asyncio
    async def test_checklists_come_from_the_issue(self):
        """There is no index, so the issue is the entry point."""
        seen = {}

        def _request(method, path, params=None, data=None):
            seen["path"] = path
            seen["params"] = params
            return {"issue": {"id": 36417, "checklists": [_checklist()]}}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await list_easy_checklists(issue_id=36417)
        assert seen["path"] == "issues/36417.json"
        assert seen["params"] == {"include": "checklists"}
        assert result["checklists"][0]["id"] == 7

    @pytest.mark.asyncio
    async def test_the_other_spelling_of_the_key_is_read_too(self):
        """Easy's include is "checklists"; its schemas say easy_checklists."""
        payload = {"issue": {"id": 1, "easy_checklists": [_checklist()]}}
        with patch.object(cl_mod, "easy_request", return_value=payload):
            result = await list_easy_checklists(issue_id=1)
        assert len(result["checklists"]) == 1

    @pytest.mark.asyncio
    async def test_an_issue_without_checklists_answers_empty(self):
        with patch.object(cl_mod, "easy_request", return_value={"issue": {"id": 1}}):
            result = await list_easy_checklists(issue_id=1)
        assert result["checklists"] == []

    @pytest.mark.asyncio
    async def test_one_checklist_is_read_directly(self):
        seen = {}

        def _request(method, path, params=None, data=None):
            seen["path"] = path
            return {"easy_checklist": _checklist()}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await list_easy_checklists(checklist_id=7)
        assert seen["path"] == "easy_checklists/7.json"
        assert result["checklists"][0]["name"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs", [{}, {"issue_id": 1, "checklist_id": 7}, {"issue_id": 0}]
    )
    async def test_the_two_ids_are_mutually_exclusive_and_checked(self, kwargs):
        with patch.object(cl_mod, "easy_request") as request:
            result = await list_easy_checklists(**kwargs)
        assert "error" in result
        request.assert_not_called()


class TestChecklistWrites:
    @pytest.mark.asyncio
    async def test_create_sends_the_entity_and_the_items_in_one_call(self):
        sent = {}

        def _request(method, path, params=None, data=None):
            sent.update(data or {})
            return {"easy_checklist": _checklist()}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await manage_easy_checklist(
                action="create",
                issue_id=36417,
                name="Definition of Done",
                items=["Review", {"subject": "Merge", "done": False}],
            )
        body = sent["easy_checklist"]
        assert body["entity_id"] == 36417
        assert body["entity_type"] == "Issue"
        assert len(body["easy_checklist_items_attributes"]) == 2
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_a_create_without_a_record_back_is_not_called_a_success(self):
        with patch.object(cl_mod, "easy_request", return_value=None):
            result = await manage_easy_checklist(
                action="create", issue_id=1, name="Liste"
            )
        assert result["code"] == "CREATE_UNCONFIRMED"
        assert "created" not in result

    @pytest.mark.asyncio
    async def test_update_reports_a_fresh_read_not_the_request(self):
        """The PUT promises no body, so the state has to be read back."""
        calls = []

        def _request(method, path, params=None, data=None):
            calls.append(method)
            if method == "put":
                return None
            return {"easy_checklist": _checklist(name="Neuer Name")}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await manage_easy_checklist(
                action="update", checklist_id=7, name="Neuer Name"
            )
        assert calls == ["put", "get"]
        assert "Neuer Name" in result["checklist"]["name"]

    @pytest.mark.asyncio
    async def test_update_needs_something_to_change(self):
        result = await manage_easy_checklist(action="update", checklist_id=7)
        assert "name or items" in result["error"]

    @pytest.mark.asyncio
    async def test_create_needs_an_issue_and_a_name(self):
        assert "issue_id" in (await manage_easy_checklist(action="create"))["error"]
        assert (
            "name"
            in (await manage_easy_checklist(action="create", issue_id=1))["error"]
        )

    @pytest.mark.asyncio
    async def test_nothing_ever_sends_destroy(self):
        """Whether _destroy works here is undocumented; deleting has its own
        endpoint, and guessing would remove entries nobody asked about."""
        sent = {}

        def _request(method, path, params=None, data=None):
            sent.update(data or {})
            return {"easy_checklist": _checklist()}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            await manage_easy_checklist(
                action="update", checklist_id=7, items=[{"id": 1, "subject": "x"}]
            )
        assert "_destroy" not in str(sent)


class TestItemWrites:
    @pytest.mark.asyncio
    async def test_creating_an_entry_answers_with_the_whole_list(self):
        """The item endpoint answers about the item; the caller wants the
        list it now belongs to."""

        def _request(method, path, params=None, data=None):
            if method == "post":
                return {"easy_checklist_item": _item(2, "Review")}
            return {"easy_checklist": _checklist(items=[_item(), _item(2, "Review")])}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await manage_easy_checklist_item(
                action="create", checklist_id=7, subject="Review"
            )
        assert result["created"] is True
        assert result["checklist"]["item_count"] == 2

    @pytest.mark.asyncio
    async def test_ticking_an_entry_off_needs_no_checklist_id(self):
        """The server names the entry's list, so the caller need not."""
        sent = {}

        def _request(method, path, params=None, data=None):
            if method == "put":
                sent.update(data or {})
                return {
                    "easy_checklist_item": {
                        "id": 1,
                        "done": True,
                        "easy_checklist": {"id": 7},
                    }
                }
            assert path == "easy_checklists/7.json"
            return {"easy_checklist": _checklist(items=[_item(done=True)])}

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await manage_easy_checklist_item(
                action="update", item_id=1, done=True
            )
        assert sent["easy_checklist_item"] == {"done": True}
        assert result["checklist"]["done_count"] == 1

    @pytest.mark.asyncio
    async def test_a_write_that_cannot_be_read_back_is_still_reported(self):
        def _request(method, path, params=None, data=None):
            if method == "post":
                return {"easy_checklist_item": _item()}
            raise ResourceNotFoundError

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await manage_easy_checklist_item(
                action="create", checklist_id=7, subject="Review"
            )
        assert result["created"] is True
        assert "could not be read back" in result["note"]
        assert "checklist" not in result

    @pytest.mark.asyncio
    async def test_update_needs_a_field(self):
        result = await manage_easy_checklist_item(action="update", item_id=1)
        assert "subject, done or position" in result["error"]

    @pytest.mark.asyncio
    async def test_create_needs_a_list_and_a_subject(self):
        assert (
            "checklist_id"
            in (await manage_easy_checklist_item(action="create"))["error"]
        )
        assert (
            "subject"
            in (await manage_easy_checklist_item(action="create", checklist_id=7))[
                "error"
            ]
        )


class TestModuleGate:
    """Checklists are a project module, and Redmine checks a module before
    any permission -- so a project without it refuses an administrator, and
    the bare 403 reads as a missing right."""

    def _forbidden_create(self, modules):
        def _request(method, path, params=None, data=None):
            if method == "post":
                raise ForbiddenError
            if path.startswith("issues/"):
                return {"issue": {"id": 39108, "project": {"id": 1225}}}
            if path.startswith("projects/"):
                return {
                    "project": {
                        "id": 1225,
                        "enabled_modules": [{"name": m} for m in modules],
                    }
                }
            raise AssertionError(path)

        return _request

    @pytest.mark.asyncio
    async def test_a_403_names_the_module_when_it_really_is_off(self):
        with patch.object(
            cl_mod,
            "easy_request",
            side_effect=self._forbidden_create(["issue_tracking", "news"]),
        ):
            result = await manage_easy_checklist(
                action="create", issue_id=39108, name="Liste"
            )
        assert result["code"] == "CHECKLIST_MODULE_DISABLED"
        assert result["issue_id"] == 39108

    @pytest.mark.asyncio
    async def test_a_plain_denial_keeps_the_plain_error(self):
        """The module is on, so this is an ordinary permission denial and
        pointing at the module would send someone the wrong way."""
        with patch.object(
            cl_mod,
            "easy_request",
            side_effect=self._forbidden_create(["issue_tracking", "easy_checklists"]),
        ):
            result = await manage_easy_checklist(
                action="create", issue_id=39108, name="Liste"
            )
        assert result.get("code") != "CHECKLIST_MODULE_DISABLED"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_an_unreadable_project_claims_nothing(self):
        def _request(method, path, params=None, data=None):
            if method == "post":
                raise ForbiddenError
            raise ResourceNotFoundError

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await manage_easy_checklist(
                action="create", issue_id=39108, name="Liste"
            )
        assert result.get("code") != "CHECKLIST_MODULE_DISABLED"


class TestDelete:
    @pytest.mark.asyncio
    async def test_deleting_a_list_previews_how_much_goes_with_it(self):
        with patch.object(
            cl_mod,
            "easy_request",
            return_value={"easy_checklist": _checklist(items=[_item(), _item(2)])},
        ) as request:
            result = await delete_easy_checklist(checklist_id=7)
        assert result["code"] == "CONFIRMATION_REQUIRED"
        assert "2 entries" in result["error"]
        assert [c.args[0] for c in request.call_args_list] == ["get"]

    @pytest.mark.asyncio
    async def test_one_entry_reads_as_singular(self):
        with patch.object(
            cl_mod,
            "easy_request",
            return_value={"easy_checklist": _checklist(items=[_item()])},
        ):
            result = await delete_easy_checklist(checklist_id=7)
        assert "1 entry" in result["error"]

    @pytest.mark.asyncio
    async def test_a_confirmed_delete_calls_delete(self):
        calls = []

        def _request(method, path, params=None, data=None):
            calls.append((method, path))
            return {"easy_checklist": _checklist()} if method == "get" else None

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await delete_easy_checklist(checklist_id=7, confirm_delete=True)
        assert calls[-1] == ("delete", "easy_checklists/7.json")
        assert result["deleted"] is True

    @pytest.mark.asyncio
    async def test_deleting_an_entry_says_it_cannot_preview_it(self):
        """An entry has no read endpoint, so the refusal cannot show it."""
        with patch.object(cl_mod, "easy_request") as request:
            result = await delete_easy_checklist(item_id=3)
        assert result["code"] == "CONFIRMATION_REQUIRED"
        assert "no read endpoint" in result["hint"]
        request.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_confirmed_entry_delete_hits_the_item_endpoint(self):
        calls = []

        def _request(method, path, params=None, data=None):
            calls.append((method, path))
            return None

        with patch.object(cl_mod, "easy_request", side_effect=_request):
            result = await delete_easy_checklist(item_id=3, confirm_delete=True)
        assert calls == [("delete", "easy_checklist_items/3.json")]
        assert result["deleted"] is True

    @pytest.mark.asyncio
    async def test_a_missing_checklist_is_named(self):
        with patch.object(cl_mod, "easy_request", return_value=None):
            result = await delete_easy_checklist(checklist_id=999, confirm_delete=True)
        assert result["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{}, {"checklist_id": 7, "item_id": 3}])
    async def test_exactly_one_id(self, kwargs):
        with patch.object(cl_mod, "easy_request") as request:
            result = await delete_easy_checklist(confirm_delete=True, **kwargs)
        assert "exactly one" in result["error"]
        request.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_only_mode_blocks_it(self):
        with patch.object(cl_mod, "is_read_only_mode", return_value=True):
            with patch.object(cl_mod, "easy_request") as request:
                result = await delete_easy_checklist(
                    checklist_id=7, confirm_delete=True
                )
        assert "error" in result
        request.assert_not_called()


class TestRegistration:
    def test_the_tools_are_mapped_and_the_family_owns_a_flag(self):
        from redmine_mcp_server._annotations import TOOL_KINDS
        from redmine_mcp_server._plugin_visibility import PLUGIN_FLAGS
        from redmine_mcp_server.oauth_scopes import TOOL_SCOPES

        for name in (
            "list_easy_checklists",
            "manage_easy_checklist",
            "manage_easy_checklist_item",
            "delete_easy_checklist",
        ):
            assert name in TOOL_SCOPES
            assert name in TOOL_KINDS
            assert "easy" in PLUGIN_FLAGS

    def test_the_names_do_not_collide_with_the_redmineup_plugin(self):
        """Different feature, different endpoints, same English word."""
        from redmine_mcp_server._annotations import TOOL_KINDS

        assert "get_checklist" in TOOL_KINDS
        assert "list_easy_checklists" in TOOL_KINDS
