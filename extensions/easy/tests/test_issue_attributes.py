"""Easy Redmine's issue attributes: read, written, and filtered on.

Easy Redmine is a fork, not a plugin. It serves the same ``/issues.json``
with extra attributes and registers extra query filters, so support is a
matter of not dropping what is already there.

These tests are the reason the extension exists. In-tree, all three sides
were a patch on ``tools/issues.py`` -- which is what conflicted on every
upstream release. Here the writing and the filtering side are two lines of
the ``ExtensionSpec``, and the reading side is nothing at all: upstream's
``unmapped_fields`` already carries a distribution's own keys. What is
tested is therefore not our code but our *registration*, which is the part
that can silently stop working.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from redmine_mcp_easy import is_enabled
from redmine_mcp_server._custom_fields import _is_standard_issue_update_key
from redmine_mcp_server._extension_registry import extension_issue_update_keys
from redmine_mcp_server.tools.issues import _issue_to_dict, _reject_issue_filters

SPRINT = {"id": 356, "name": "PGMS Sprint 26-34", "due_date": "2026-08-23"}

WRITABLE = ("easy_sprint_id", "easy_story_points", "target_backlog")


def _issue(**extra):
    """A python-redmine-ish issue: attribute access plus a raw() payload."""
    payload = dict(id=36417, subject="Cleanup-Review", **extra)
    issue = SimpleNamespace(**payload)
    issue.raw = lambda: dict(payload)
    return issue


@pytest.fixture
def easy_on(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_ENABLED", "true")


@pytest.fixture
def easy_off(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_ENABLED", "false")


# --- the family's flag ---------------------------------------------------


def test_the_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv("REDMINE_EASY_ENABLED", raising=False)
    assert is_enabled() is False


def test_the_flag_reads_the_usual_truthy_spellings(monkeypatch):
    for value in ("true", "True", "1", "yes", "on"):
        monkeypatch.setenv("REDMINE_EASY_ENABLED", value)
        assert is_enabled() is True, value
    for value in ("false", "0", "no", "", "nonsense"):
        monkeypatch.setenv("REDMINE_EASY_ENABLED", value)
        assert is_enabled() is False, value


# --- reading: upstream's unmapped_fields, not ours -----------------------


def test_the_sprint_comes_back_under_unmapped_fields(easy_on):
    """No registration needed. `_issue_unmapped_fields` reads the payload for
    exactly this case -- a distribution's own top-level keys."""
    sprint = _issue_to_dict(_issue(easy_sprint=SPRINT))["unmapped_fields"][
        "easy_sprint"
    ]

    assert sprint["id"] == 356
    assert "PGMS Sprint 26-34" in sprint["name"]
    assert "2026-08-23" in sprint["due_date"]


def test_its_strings_arrive_wrapped(easy_on):
    """The other thing the extension gave up, and the better half of the
    trade: a sprint name is prose somebody typed, and `unmapped_fields`
    wraps it against prompt injection like every other user-authored string.
    Our own serializer passed the name through unwrapped."""
    sprint = _issue_to_dict(_issue(easy_sprint=SPRINT))["unmapped_fields"][
        "easy_sprint"
    ]

    assert sprint["name"].startswith("<insecure-content-")
    # The id is a number and stays one: a caller has to be able to pass it
    # straight back as easy_sprint_id.
    assert sprint["id"] == 356


def test_reading_does_not_depend_on_the_flag(easy_off):
    """The flag governs this family's *tools*, not what Redmine happens to
    send. A serializer that dropped a key because a flag was off would be
    hiding data the server already received."""
    result = _issue_to_dict(_issue(easy_sprint=SPRINT))

    assert result["unmapped_fields"]["easy_sprint"]["id"] == 356


def test_a_stock_redmine_issue_carries_no_such_key(easy_on):
    result = _issue_to_dict(_issue())

    assert "easy_sprint" not in result.get("unmapped_fields", {})


def test_the_sprint_is_not_a_top_level_key(easy_on):
    """The shape the extension gave up. In-tree this was a top-level
    `easy_sprint` normalized to {id, name, due_date}; here it is Redmine's
    own payload, one level down. Pinned so a reader knows where to look and
    a future change has to say so out loud."""
    result = _issue_to_dict(_issue(easy_sprint=SPRINT))

    assert "easy_sprint" not in result


# --- writing: issue_update_keys ------------------------------------------


def test_the_writable_keys_count_as_standard_fields(easy_on):
    """Registered rather than left to the custom-field path on purpose:
    `_normalize_field_label` strips non-alphanumerics, so a custom field
    named "Easy Sprint ID" normalizes to the same `easysprintid` as
    `easy_sprint_id` and would silently swallow the value."""
    for key in WRITABLE:
        assert _is_standard_issue_update_key(key) is True, key


def test_they_are_not_standard_fields_with_the_flag_off(easy_off):
    for key in WRITABLE:
        assert _is_standard_issue_update_key(key) is False, key


def test_the_writable_keys_are_the_documented_ones(easy_on):
    """From IssueApiRequest in the instance's own /easy_swagger.json. A name
    that is not there reaches Redmine as an attribute it does not know."""
    assert set(extension_issue_update_keys()) == set(WRITABLE)


# --- filtering: issue_query_filters --------------------------------------


def test_the_sprint_filter_is_refused_when_the_flag_is_off(easy_off):
    error = _reject_issue_filters({"easy_sprint_id": 356})

    assert error is not None
    assert "easy_sprint_id" in error


def test_the_sprint_filter_is_accepted_when_the_flag_is_on(easy_on):
    assert _reject_issue_filters({"easy_sprint_id": 356}) is None


def test_only_verified_filter_names_are_accepted(easy_on):
    """An unregistered name is dropped by Redmine, which answers 200 with the
    collection unnarrowed -- indistinguishable from a filter that matched
    everything. So the list holds only what was checked against a live
    instance."""
    assert _reject_issue_filters({"easy_made_up_id": 1}) is not None


def test_the_scalar_rule_still_applies_to_easy_filters(easy_on):
    assert _reject_issue_filters({"easy_sprint_id": {"nested": 1}}) is not None


@pytest.mark.asyncio
async def test_the_companion_parameter_rides_along(easy_on):
    """Easy replaces Redmine's filter handling with EasyQuery, which engages
    on `set_filter`. Without it the filter is dropped."""
    from redmine_mcp_server.tools.issues import list_redmine_issues

    with patch("redmine_mcp_server._client.redmine") as mock_redmine:
        mock_redmine.issue.filter.return_value = []
        await list_redmine_issues(filters={"easy_sprint_id": 356})

        sent = mock_redmine.issue.filter.call_args.kwargs
        assert sent["easy_sprint_id"] == 356
        assert sent["set_filter"] == 1


@pytest.mark.asyncio
async def test_it_does_not_ride_along_unasked(easy_on):
    """A query naming no Easy filter goes out exactly as it did before."""
    from redmine_mcp_server.tools.issues import list_redmine_issues

    with patch("redmine_mcp_server._client.redmine") as mock_redmine:
        mock_redmine.issue.filter.return_value = []
        await list_redmine_issues(project_id=1)

        assert "set_filter" not in mock_redmine.issue.filter.call_args.kwargs
