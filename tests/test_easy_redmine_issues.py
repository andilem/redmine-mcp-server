"""Easy Redmine's issue attributes: read, written, and filtered on.

Easy Redmine is a fork, not a plugin. It serves the same ``/issues.json``
with extra attributes and registers extra query filters, so support is a
matter of not dropping what is already there -- gated on
``REDMINE_EASY_ENABLED`` so a stock Redmine sees none of it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from redmine_mcp_server._custom_fields import _is_standard_issue_update_key
from redmine_mcp_server._env import _is_easy_enabled
from redmine_mcp_server.tools.issues import (
    _EASY_QUERY_FILTER_NAMES,
    _EASY_WRITABLE_KEYS,
    _easy_issue_fields,
    _easy_sprint_to_dict,
    _issue_to_dict,
    _issue_to_dict_selective,
    _reject_issue_filters,
)


def _issue(**extra):
    """A python-redmine-ish issue: attribute access, missing keys absent."""
    base = dict(
        id=36417,
        subject="PGMS Self Registration",
        description="",
        project=SimpleNamespace(id=1291, name="FTTA Maintenance & Support"),
        status=SimpleNamespace(id=16, name="WORKING"),
        priority=SimpleNamespace(id=4, name="normal"),
        tracker=SimpleNamespace(id=211, name="TICKET"),
        author=SimpleNamespace(id=999, name="Anonymous"),
        assigned_to=SimpleNamespace(id=108, name="Andreas Lemmer"),
        category=None,
        fixed_version=None,
        parent=None,
        done_ratio=100,
        estimated_hours=4,
        spent_hours=0.5,
        is_private=False,
    )
    base.update(extra)
    return SimpleNamespace(**base)


SPRINT = {"id": 812, "name": "PGMS 26-34", "due_date": "2026-08-23"}


@pytest.fixture
def easy_on(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_ENABLED", "true")


@pytest.fixture(autouse=True)
def easy_off_by_default(monkeypatch):
    monkeypatch.delenv("REDMINE_EASY_ENABLED", raising=False)


# --- the flag -----------------------------------------------------------


def test_flag_defaults_to_off():
    assert _is_easy_enabled() is False


def test_flag_reads_the_usual_truthy_spellings(monkeypatch):
    for value in ("true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("REDMINE_EASY_ENABLED", value)
        assert _is_easy_enabled() is True, value
    for value in ("false", "0", "", "no"):
        monkeypatch.setenv("REDMINE_EASY_ENABLED", value)
        assert _is_easy_enabled() is False, value


# --- reading ------------------------------------------------------------


def test_sprint_is_absent_when_the_flag_is_off():
    result = _issue_to_dict(_issue(easy_sprint=SPRINT))
    assert "easy_sprint" not in result


def test_sprint_is_read_when_the_flag_is_on(easy_on):
    result = _issue_to_dict(_issue(easy_sprint=SPRINT))
    assert result["easy_sprint"] == SPRINT


def test_an_issue_in_no_sprint_reads_as_none(easy_on):
    assert _issue_to_dict(_issue(easy_sprint=None))["easy_sprint"] is None


def test_a_stock_redmine_issue_reads_as_none(easy_on):
    """The attribute is simply absent there; that must not raise."""
    result = _issue_to_dict(_issue())
    assert result["easy_sprint"] is None
    assert result["easy_story_points"] is None


def test_the_other_sprint_attributes_come_along(easy_on):
    result = _issue_to_dict(
        _issue(
            easy_sprint=SPRINT,
            easy_sprint_phase=2,
            easy_sprint_position=7,
            easy_story_points="5",
        )
    )
    assert result["easy_sprint_phase"] == 2
    assert result["easy_sprint_position"] == 7
    assert result["easy_story_points"] == "5"


def test_sprint_survives_an_attribute_bag_instead_of_a_dict(easy_on):
    """Which shape arrives depends on the call path, so read both."""
    bag = SimpleNamespace(id=812, name="PGMS 26-34", due_date="2026-08-23")
    assert _easy_sprint_to_dict(bag) == SPRINT


def test_a_sprint_without_an_id_is_not_a_sprint(easy_on):
    assert _easy_sprint_to_dict({"name": "PGMS 26-34"}) is None
    assert _easy_sprint_to_dict(None) is None


def test_selective_fields_can_name_the_sprint(easy_on):
    result = _issue_to_dict_selective(
        _issue(easy_sprint=SPRINT), ["id", "subject", "easy_sprint"]
    )
    assert result == {
        "id": 36417,
        "subject": "PGMS Self Registration",
        "easy_sprint": SPRINT,
    }


def test_naming_the_sprint_selects_nothing_with_the_flag_off():
    result = _issue_to_dict_selective(_issue(easy_sprint=SPRINT), ["id", "easy_sprint"])
    assert result == {"id": 36417}


def test_the_star_selector_includes_the_sprint(easy_on):
    result = _issue_to_dict_selective(_issue(easy_sprint=SPRINT), ["*"])
    assert result["easy_sprint"] == SPRINT


def test_easy_issue_fields_is_empty_when_off():
    assert _easy_issue_fields(_issue(easy_sprint=SPRINT)) == {}


# --- writing ------------------------------------------------------------


def test_the_writable_keys_count_as_standard_fields():
    """Otherwise they are treated as custom-field names.

    ``_normalize_field_label`` strips underscores, so a custom field called
    "Easy Sprint ID" normalizes to the same string as ``easy_sprint_id`` and
    would silently swallow the value instead of setting the sprint.
    """
    for key in _EASY_WRITABLE_KEYS:
        assert _is_standard_issue_update_key(key), key


def test_the_writable_keys_are_the_documented_ones():
    assert set(_EASY_WRITABLE_KEYS) == {
        "easy_sprint_id",
        "easy_story_points",
        "target_backlog",
    }


# --- filtering ----------------------------------------------------------


def test_sprint_filter_is_refused_when_the_flag_is_off():
    error = _reject_issue_filters({"easy_sprint_id": 812})
    assert error is not None
    assert "easy_sprint_id" in error


def test_sprint_filter_is_accepted_when_the_flag_is_on(easy_on):
    assert _reject_issue_filters({"easy_sprint_id": 812}) is None


def test_only_verified_filter_names_are_accepted(easy_on):
    """An unregistered name is dropped by Redmine, which then answers 200
    with the collection unnarrowed -- indistinguishable from a filter that
    matched everything. So the allow list holds only what was checked."""
    assert _EASY_QUERY_FILTER_NAMES == frozenset({"easy_sprint_id"})
    assert _reject_issue_filters({"easy_story_points": "5"}) is not None


def test_the_scalar_rule_still_applies_to_easy_filters(easy_on):
    assert _reject_issue_filters({"easy_sprint_id": [812, 813]}) is not None


@pytest.mark.asyncio
async def test_a_sprint_filter_sends_set_filter(easy_on):
    """Easy Query engages on set_filter; Redmine's API path ignores it."""
    from redmine_mcp_server.tools import issues as issues_mod

    captured = {}

    class FakeIssueManager:
        def filter(self, **params):
            captured.update(params)
            return []

    with patch.object(
        issues_mod,
        "_get_redmine_client",
        return_value=SimpleNamespace(issue=FakeIssueManager()),
    ):
        await issues_mod.list_redmine_issues(filters={"easy_sprint_id": 812})

    assert captured["easy_sprint_id"] == 812
    assert captured["set_filter"] == 1


@pytest.mark.asyncio
async def test_a_stock_request_is_left_alone():
    from redmine_mcp_server.tools import issues as issues_mod

    captured = {}

    class FakeIssueManager:
        def filter(self, **params):
            captured.update(params)
            return []

    with patch.object(
        issues_mod,
        "_get_redmine_client",
        return_value=SimpleNamespace(issue=FakeIssueManager()),
    ):
        await issues_mod.list_redmine_issues(project_id=1291)

    assert "set_filter" not in captured
