"""Pin the MCP sidecar's per-tool proxy timeout selection: run_eval / kb_leak_audit
must get the long knowledge-stack allowance (they drive several 60-120s LLM calls
per request), everything else the snappy 30s default. A flat 30s regressed run_eval
by aborting the sidecar mid-run while the backend kept billing the discarded work.

The sidecar (mcp/main.py) is a standalone image with no test harness of its own and
isn't part of the backend package, so compose.dev mounts it read-only at
/app/mcp_src and this test loads it by file path; it skips cleanly wherever that
mount is absent (e.g. the built api/prod image)."""
import importlib.util
import pathlib

import pytest

MCP_MAIN = pathlib.Path("/app/mcp_src/main.py")


@pytest.fixture(scope="module")
def mcp_main():
    if not MCP_MAIN.exists():
        pytest.skip("mcp sidecar source not mounted at /app/mcp_src")
    spec = importlib.util.spec_from_file_location("mcp_main_under_test", MCP_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_long_running_tools_get_extended_timeout(mcp_main):
    assert mcp_main.HUB_TOOL_TIMEOUTS["run_eval"] == 150.0
    assert mcp_main.HUB_TOOL_TIMEOUTS["kb_leak_audit"] == 130.0


def test_default_timeout_for_snappy_tools(mcp_main):
    assert mcp_main.HUB_TOOL_DEFAULT_TIMEOUT == 30.0
    for name in ("spoke_info", "usage_summary", "list_credit_events",
                 "find_org", "grant_credits"):
        assert mcp_main._hub_timeout(name) == 30.0


def test_timeout_selection_matches_map(mcp_main):
    assert mcp_main._hub_timeout("run_eval") == 150.0
    assert mcp_main._hub_timeout("kb_leak_audit") == 130.0
    # An unrouted / unknown tool name falls back to the default.
    assert mcp_main._hub_timeout("unknown_tool") == mcp_main.HUB_TOOL_DEFAULT_TIMEOUT
    # Every routed hub tool resolves to a positive timeout.
    for name in mcp_main.HUB_TOOL_ROUTES:
        assert mcp_main._hub_timeout(name) > 0
