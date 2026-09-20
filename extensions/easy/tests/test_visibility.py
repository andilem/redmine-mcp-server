"""The family flag decides whether the tools are reachable.

This is what replaced the in-tree registration guard. There, each module
ended with ``if _is_easy_enabled(): tool = mcp.tool()(tool)`` -- the tool
did not exist at all when the flag was off, which is why every name had to
be listed in ``CONDITIONALLY_REGISTERED`` or a correct spelling in
``allowed-tools.txt`` read as a typo. Here the tools are always defined and
tagged ``plugin:easy``, and ``apply_plugin_visibility`` disables the tag.

The check runs in a subprocess on purpose. It needs ``redmine_mcp_server.main``,
whose import loads the extensions, applies visibility and installs
middleware once -- doing that inside the test session would leave the other
tests looking at a server configured by whichever test ran first.
"""

import subprocess
import sys

import pytest

PROBE = """
import asyncio
import redmine_mcp_server.main  # noqa: F401  -- loads extensions, applies visibility
from redmine_mcp_server.server import mcp

NAMES = [
    "list_easy_sprints",
    "list_easy_attendances",
    "manage_easy_attendance",
    "delete_easy_attendance",
    "approve_easy_attendances",
    "list_easy_checklists",
    "manage_easy_checklist",
    "manage_easy_checklist_item",
    "delete_easy_checklist",
]


async def main():
    reachable = [n for n in NAMES if await mcp.get_tool(n) is not None]
    print("REACHABLE", len(reachable), len(NAMES))
    # A built-in tool proves the server came up rather than the probe
    # failing in a way that happens to look like "all hidden".
    print("CONTROL", await mcp.get_tool("get_redmine_issue") is not None)


asyncio.run(main())
"""


def _probe(flag: str) -> dict:
    env = {
        "PATH": "",
        "SYSTEMROOT": "",
        "REDMINE_URL": "https://example.invalid",
        "REDMINE_API_KEY": "x",
        "REDMINE_MCP_EXTENSIONS": "redmine_mcp_easy",
        "REDMINE_EASY_ENABLED": flag,
    }
    import os

    env["PATH"] = os.environ.get("PATH", "")
    env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    out = {}
    for line in result.stdout.splitlines():
        if line.startswith("REACHABLE"):
            _, got, total = line.split()
            out["reachable"] = (int(got), int(total))
        elif line.startswith("CONTROL"):
            out["control"] = line.split()[1] == "True"
    return out


@pytest.mark.slow
def test_the_tools_are_reachable_with_the_flag_on():
    result = _probe("true")

    assert result["control"] is True
    assert result["reachable"] == (9, 9)


@pytest.mark.slow
def test_none_of_them_is_reachable_with_the_flag_off():
    """Loaded but hidden -- the extension is imported either way, and only
    `apply_plugin_visibility` decides. A stock Redmine has none of these
    endpoints, so a visible tool could only ever fail."""
    result = _probe("false")

    assert result["control"] is True, "the server itself must still be up"
    assert result["reachable"] == (0, 9)
