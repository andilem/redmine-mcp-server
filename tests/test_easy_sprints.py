"""The sprint lookup: its SQL, and who is allowed to see the result."""

import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from redmine_mcp_server import _easy_db
from redmine_mcp_server._easy_db import (
    _connection_params,
    _row_to_sprint,
    build_sprint_query,
    is_configured,
)
from redmine_mcp_server._tool_allow_list import CONDITIONALLY_REGISTERED
from redmine_mcp_server.tools import easy_sprints as sprints_mod

DSN = "mysql://reader:s3cr3t@db.internal:3307/easyredmine"


@pytest.fixture(autouse=True)
def _no_ambient_dsn(monkeypatch):
    monkeypatch.delenv("REDMINE_EASY_DB_URL", raising=False)


@pytest.fixture
def dsn(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_DB_URL", DSN)


def _row(**extra):
    base = {
        "id": 812,
        "name": "PGMS 26-34",
        "start_date": datetime.date(2026, 8, 17),
        "due_date": datetime.date(2026, 8, 23),
        "closed": 0,
        "cross_project": 0,
        "capacity": 40,
        "goal": "Self-Registration fixen",
        "version_id": None,
        "project_id": 1291,
    }
    base.update(extra)
    return base


# --- configuration ------------------------------------------------------


def test_unconfigured_by_default():
    assert is_configured() is False


def test_configured_when_the_dsn_is_set(dsn):
    assert is_configured() is True


def test_a_blank_dsn_counts_as_unset(monkeypatch):
    """docker-compose renders an unset ${VAR} as the empty string."""
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "   ")
    assert is_configured() is False


def test_the_dsn_is_split_into_driver_arguments(dsn):
    params = _connection_params()
    assert params["host"] == "db.internal"
    assert params["port"] == 3307
    assert params["user"] == "reader"
    assert params["password"] == "s3cr3t"
    assert params["database"] == "easyredmine"
    assert params["charset"] == "utf8mb4"


def test_timeouts_are_always_set(dsn):
    """A hung database must not hang the MCP server."""
    params = _connection_params()
    assert params["connect_timeout"] > 0
    assert params["read_timeout"] > 0


def test_a_sqlalchemy_style_scheme_is_accepted(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "mysql+pymysql://u:p@h/db")
    assert _connection_params()["database"] == "db"


def test_a_percent_encoded_password_is_decoded(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "mysql://u:p%40ss%3A1@h/db")
    assert _connection_params()["password"] == "p@ss:1"


def test_a_non_mysql_scheme_is_refused(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "postgresql://u:p@h/db")
    with pytest.raises(RuntimeError, match="mysql"):
        _connection_params()


def test_a_dsn_without_a_database_is_refused(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "mysql://u:p@h")
    with pytest.raises(RuntimeError, match="names no database"):
        _connection_params()


def test_a_dsn_without_a_host_is_refused(monkeypatch):
    """No silent localhost fallback: in a container that is the container."""
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "mysql:///easyredmine")
    with pytest.raises(RuntimeError, match="names no host"):
        _connection_params()


def test_the_missing_host_message_names_the_container_trap(monkeypatch):
    monkeypatch.setenv("REDMINE_EASY_DB_URL", "mysql:///db")
    with pytest.raises(RuntimeError, match="inside the container"):
        _connection_params()


def test_an_unset_dsn_says_what_to_set():
    with pytest.raises(RuntimeError, match="REDMINE_EASY_DB_URL"):
        _connection_params()


# --- the SQL ------------------------------------------------------------


def test_open_sprints_are_the_default():
    sql, params = build_sprint_query()
    assert "s.closed = %s" in sql
    assert params[0] == 0


def test_closed_none_asks_about_both():
    sql, params = build_sprint_query(closed=None)
    assert "s.closed = %s" not in sql
    assert params == [25, 0]  # only the window is left


def test_active_on_bounds_the_date_from_both_sides():
    sql, params = build_sprint_query(active_on="2026-09-02")
    assert "s.start_date <= %s AND s.due_date >= %s" in sql
    assert params.count("2026-09-02") == 2


def test_a_name_is_matched_as_a_substring():
    _, params = build_sprint_query(name="26-34")
    assert "%26-34%" in params


def test_a_project_filter_keeps_the_global_sprints():
    """A cross-project sprint is not in the project but applies to it."""
    sql, params = build_sprint_query(project_id=1291)
    assert "s.cross_project = 1" in sql
    assert "s.project_id IS NULL" in sql
    assert 1291 in params


def test_the_window_is_the_last_two_parameters():
    _, params = build_sprint_query(name="x", limit=10, offset=20)
    assert params[-2:] == [10, 20]


def test_no_caller_value_reaches_the_statement():
    """The whole point of the named-query rule."""
    nasty = "'; DROP TABLE easy_sprints; --"
    sql, params = build_sprint_query(name=nasty, project_id=7, active_on="2026-01-01")
    assert nasty not in sql
    assert "DROP" not in sql.upper()
    assert f"%{nasty}%" in params
    # Every variable part is a placeholder, and there is one per parameter.
    assert sql.count("%s") == len(params)


def test_the_statement_only_reads():
    sql, _ = build_sprint_query()
    assert sql.strip().upper().startswith("SELECT")
    assert "easy_sprints" in sql


def test_sprints_without_a_due_date_sort_last():
    sql, _ = build_sprint_query()
    assert "s.due_date IS NULL" in sql


# --- row conversion -----------------------------------------------------


def test_dates_come_back_as_iso_strings():
    sprint = _row_to_sprint(_row())
    assert sprint["start_date"] == "2026-08-17"
    assert sprint["due_date"] == "2026-08-23"


def test_tinyints_come_back_as_booleans():
    sprint = _row_to_sprint(_row(closed=1, cross_project=1))
    assert sprint["closed"] is True
    assert sprint["cross_project"] is True


def test_the_goal_is_wrapped_as_untrusted_content():
    """It is prose a user typed, and it arrives as HTML."""
    sprint = _row_to_sprint(_row(goal="<p>Ganz wichtig: Touroptimierung</p>"))
    assert sprint["goal"].startswith("<insecure-content-")
    assert "Touroptimierung" in sprint["goal"]


def test_an_empty_goal_is_left_alone():
    assert _row_to_sprint(_row(goal=""))["goal"] == ""


def test_the_sprint_name_is_not_wrapped():
    """Names are matched, compared and echoed back; a boundary tag there
    would break resolving what the user typed."""
    assert _row_to_sprint(_row())["name"] == "PGMS 26-34"


def test_null_dates_survive_conversion():
    sprint = _row_to_sprint(_row(start_date=None, due_date=None))
    assert sprint["start_date"] is None


# --- the tool -----------------------------------------------------------


def _client(visible_ids):
    """A Redmine client where only ``visible_ids`` can be fetched."""

    class Projects:
        def get(self, project_id):
            if project_id not in visible_ids:
                raise Exception("403 Forbidden")
            return SimpleNamespace(id=project_id, name=f"Projekt {project_id}")

    return SimpleNamespace(project=Projects())


@pytest.mark.asyncio
async def test_without_a_dsn_the_tool_says_what_is_missing():
    result = await sprints_mod.list_easy_sprints()
    assert result["code"] == "EASY_DB_NOT_CONFIGURED"
    assert "REDMINE_EASY_DB_URL" in result["error"]
    # Setting a sprint does not need the database, and the hint says so.
    assert "by id" in result["hint"]


@pytest.mark.asyncio
async def test_a_bad_limit_is_refused_before_the_database(dsn):
    with patch.object(sprints_mod, "fetch_sprints") as fetch:
        result = await sprints_mod.list_easy_sprints(limit=500)
    assert "limit" in result["error"]
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_a_negative_offset_is_refused(dsn):
    result = await sprints_mod.list_easy_sprints(offset=-1)
    assert "offset" in result["error"]


@pytest.mark.asyncio
async def test_a_visible_sprint_comes_back_with_its_project(dsn):
    sprint = _row_to_sprint(_row())
    with patch.object(sprints_mod, "fetch_sprints", return_value=[sprint]):
        with patch.object(
            sprints_mod, "_get_redmine_client", return_value=_client({1291})
        ):
            result = await sprints_mod.list_easy_sprints(name="26-34")
    assert len(result["sprints"]) == 1
    assert result["sprints"][0]["project"] == {"id": 1291, "name": "Projekt 1291"}
    assert "project_id" not in result["sprints"][0]


@pytest.mark.asyncio
async def test_a_sprint_in_an_invisible_project_is_dropped(dsn):
    """The database read has no permissions; the caller's key decides."""
    rows = [_row_to_sprint(_row()), _row_to_sprint(_row(id=99, project_id=4242))]
    with patch.object(sprints_mod, "fetch_sprints", return_value=rows):
        with patch.object(
            sprints_mod, "_get_redmine_client", return_value=_client({1291})
        ):
            result = await sprints_mod.list_easy_sprints()
    assert [s["id"] for s in result["sprints"]] == [812]


@pytest.mark.asyncio
async def test_a_project_less_sprint_is_visible_to_everyone(dsn):
    rows = [_row_to_sprint(_row(project_id=None))]
    with patch.object(sprints_mod, "fetch_sprints", return_value=rows):
        with patch.object(
            sprints_mod, "_get_redmine_client", return_value=_client(set())
        ):
            result = await sprints_mod.list_easy_sprints()
    assert result["sprints"][0]["project"] is None


@pytest.mark.asyncio
async def test_each_project_is_checked_once(dsn):
    """Two sprints in one project must not cost two visibility checks."""
    rows = [_row_to_sprint(_row()), _row_to_sprint(_row(id=813))]
    calls = []

    class Projects:
        def get(self, project_id):
            calls.append(project_id)
            return SimpleNamespace(id=project_id, name="FTTA")

    with patch.object(sprints_mod, "fetch_sprints", return_value=rows):
        with patch.object(
            sprints_mod,
            "_get_redmine_client",
            return_value=SimpleNamespace(project=Projects()),
        ):
            result = await sprints_mod.list_easy_sprints()
    assert calls == [1291]
    assert len(result["sprints"]) == 2


@pytest.mark.asyncio
async def test_a_missing_driver_is_reported_as_such(dsn):
    with patch.object(
        sprints_mod,
        "fetch_sprints",
        side_effect=RuntimeError("needs the PyMySQL driver"),
    ):
        result = await sprints_mod.list_easy_sprints()
    assert result["code"] == "EASY_DB_UNAVAILABLE"
    assert "PyMySQL" in result["error"]


@pytest.mark.asyncio
async def test_a_database_outage_does_not_raise_through_the_tool(dsn):
    with patch.object(
        sprints_mod, "fetch_sprints", side_effect=OSError("connection refused")
    ):
        result = await sprints_mod.list_easy_sprints()
    assert result["code"] == "EASY_DB_ERROR"
    assert "connection refused" in result["error"]


# --- registration -------------------------------------------------------


def test_the_tool_is_exempt_from_the_allow_list_typo_warning():
    """It only registers under REDMINE_EASY_ENABLED, so a correct spelling
    can legitimately be absent from the component registry."""
    assert "list_easy_sprints" in CONDITIONALLY_REGISTERED


def test_the_module_exposes_no_raw_sql_entry_point():
    """A "run this SQL" tool would hand every caller users.hashed_password.

    So the only public entry points are named queries, and the one that
    builds SQL hands it back rather than running it.
    """
    import inspect

    callables = {
        name
        for name in dir(_easy_db)
        if not name.startswith("_") and callable(getattr(_easy_db, name))
        # Imports land in dir() too; only what this module defines counts.
        and getattr(getattr(_easy_db, name), "__module__", None) == _easy_db.__name__
    }
    assert callables == {
        "build_sprint_query",
        "fetch_sprints",
        "database_url",
        "is_configured",
    }
    # Nothing public takes a statement from its caller.
    for name in callables:
        params = inspect.signature(getattr(_easy_db, name)).parameters
        assert "sql" not in params, name
    # The executing function is private and stays that way.
    assert not hasattr(_easy_db, "select")
    assert callable(_easy_db._select)
