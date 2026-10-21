"""Tests for `notes_upload_id` on `update_redmine_issue`.

#317 let a long note be *replaced* from a staged file, through
`manage_issue_note`. A new comment still had to be written out into
`fields["notes"]`, and writing a short one first and replacing it is no way
round that: the note goes out by mail to the watchers the moment it is
written. This is the route for the first version, and like the edit route it
reports a length and a digest instead of echoing the text back.
"""

import hashlib
from unittest.mock import Mock, patch

import pytest

from redmine_mcp_server import _client, _upload_store
from redmine_mcp_server.tools.issues import update_redmine_issue

NOTE = "<p>Ein langer Kommentar mit Ümläuten.</p>\n" * 200


@pytest.fixture
def attachments_dir(tmp_path, monkeypatch):
    path = tmp_path / "attachments"
    path.mkdir()
    monkeypatch.setenv("ATTACHMENTS_DIR", str(path))
    monkeypatch.delenv("REDMINE_MCP_READ_ONLY", raising=False)
    return path


@pytest.fixture
def mock_redmine():
    with patch("redmine_mcp_server._client.redmine") as mock:
        issue = mock.issue.get.return_value
        issue.raw.return_value = {"id": 7}
        yield mock


def _stage(text: bytes, owner: str | None = None) -> str:
    issued = _upload_store.create_ticket(filename="note.html", owner=owner)
    record, reason = _upload_store.redeem_ticket(issued["upload_id"], issued["ticket"])
    assert reason is None
    _upload_store.staged_path(record).write_bytes(text)
    _upload_store.mark_ready(issued["upload_id"], record, len(text), "x")
    return issued["upload_id"]


@pytest.mark.unit
class TestStagedComment:
    @pytest.mark.asyncio
    async def test_the_staged_text_reaches_redmine_as_the_note(
        self, attachments_dir, mock_redmine
    ):
        upload_id = _stage(NOTE.encode("utf-8"))

        await update_redmine_issue(
            issue_id=7,
            fields={"private_notes": True},
            notes_upload_id=upload_id,
        )

        sent = mock_redmine.issue.update.call_args
        assert sent.kwargs["notes"] == NOTE
        assert sent.kwargs["private_notes"] is True

    @pytest.mark.asyncio
    async def test_the_note_is_reported_not_echoed(self, attachments_dir, mock_redmine):
        upload_id = _stage(NOTE.encode("utf-8"))

        result = await update_redmine_issue(
            issue_id=7, fields={}, notes_upload_id=upload_id
        )

        assert "error" not in result
        assert result["notes_length"] == len(NOTE)
        assert (
            result["notes_sha256"] == hashlib.sha256(NOTE.encode("utf-8")).hexdigest()
        )
        assert "Ein langer Kommentar" not in str(result)

    @pytest.mark.asyncio
    async def test_other_fields_still_ride_along(self, attachments_dir, mock_redmine):
        upload_id = _stage(b"<p>kurz</p>")

        await update_redmine_issue(
            issue_id=7, fields={"done_ratio": 50}, notes_upload_id=upload_id
        )

        sent = mock_redmine.issue.update.call_args
        assert sent.kwargs["done_ratio"] == 50
        assert sent.kwargs["notes"] == "<p>kurz</p>"

    @pytest.mark.asyncio
    async def test_it_combines_with_a_staged_description(
        self, attachments_dir, mock_redmine
    ):
        notes_id = _stage(b"<p>der Kommentar</p>")
        description_id = _stage(b"<p>die Beschreibung</p>")

        await update_redmine_issue(
            issue_id=7,
            fields={},
            notes_upload_id=notes_id,
            description_upload_id=description_id,
        )

        sent = mock_redmine.issue.update.call_args
        assert sent.kwargs["notes"] == "<p>der Kommentar</p>"
        assert sent.kwargs["description"] == "<p>die Beschreibung</p>"

    @pytest.mark.asyncio
    async def test_a_failed_write_reports_no_digest(
        self, attachments_dir, mock_redmine
    ):
        upload_id = _stage(b"<p>kurz</p>")
        mock_redmine.issue.update.side_effect = RuntimeError("boom")

        result = await update_redmine_issue(
            issue_id=7, fields={}, notes_upload_id=upload_id
        )

        assert "error" in result
        assert "notes_sha256" not in result


@pytest.mark.unit
class TestRefusedBeforeWriting:
    @pytest.mark.asyncio
    async def test_both_sources_at_once_are_refused(
        self, attachments_dir, mock_redmine
    ):
        upload_id = _stage(b"<p>neu</p>")

        result = await update_redmine_issue(
            issue_id=7, fields={"notes": "<p>auch</p>"}, notes_upload_id=upload_id
        )

        assert "one way at a time" in result["error"]
        mock_redmine.issue.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_utf8_is_reported(self, attachments_dir, mock_redmine):
        upload_id = _stage(bytes([0xFF, 0xFE]) + b" not text")

        result = await update_redmine_issue(
            issue_id=7, fields={}, notes_upload_id=upload_id
        )

        assert "not valid UTF-8" in result["error"]
        mock_redmine.issue.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unsent_upload_is_reported(self, attachments_dir, mock_redmine):
        issued = _upload_store.create_ticket(filename="note.html")

        result = await update_redmine_issue(
            issue_id=7, fields={}, notes_upload_id=issued["upload_id"]
        )

        assert "error" in result
        mock_redmine.issue.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_someone_else_s_upload_reads_as_unknown(
        self, attachments_dir, mock_redmine
    ):
        upload_id = _stage(b"<p>fremd</p>", owner="redmine-user:5")
        token = Mock()
        token.claims = {"redmine_user_id": 6}

        with (
            patch.object(_client, "REDMINE_AUTH_MODE", "api-key-login"),
            patch("redmine_mcp_server._caller.get_access_token", return_value=token),
        ):
            result = await update_redmine_issue(
                issue_id=7, fields={}, notes_upload_id=upload_id
            )

        assert result["error"].startswith(f"No staged upload {upload_id}.")
        mock_redmine.issue.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_only_mode_refuses_it(
        self, attachments_dir, mock_redmine, monkeypatch
    ):
        upload_id = _stage(b"<p>neu</p>")
        monkeypatch.setenv("REDMINE_MCP_READ_ONLY", "true")

        result = await update_redmine_issue(
            issue_id=7, fields={}, notes_upload_id=upload_id
        )

        assert "error" in result
        mock_redmine.issue.update.assert_not_called()
