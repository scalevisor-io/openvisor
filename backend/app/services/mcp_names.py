"""What a knowledge source or tool is CALLED inside a dev run.

The agent addresses an MCP server by a key in its `mcp.json`, and that key is
derived from a display name or a §Tools slug - so the Staan provider is
`websearch_staan` to the agent and to whoever writes project instructions
mentioning it. The derivation lived only in the dispatcher, which meant the
admin pages could show a source without ever showing the one string you have to
type to refer to it.

Both the dispatcher and the admin API resolve names here, so what the UI
displays is what the run will use.
"""
import re

# Server keys the dispatcher reserves before any KB/tool is named.
RESERVED = ("browser", "context7")

# Tools we ship ourselves, so we can name them without probing anything. A
# third-party MCP server's tool names are its own business (we only vet them at
# enable time) - the server key is what the UI can promise.
KNOWN_TOOLS = {
    "context7": ("resolve-library-id", "get-library-docs"),
}

# Kinds that are retrieval sources, not callable tools: their content reaches the
# agent through the task's knowledge section, never by name.
RETRIEVAL_KINDS = ("local", "git")


def server_name(raw: str, used: set | None = None) -> str:
    """Slug a display name into a safe MCP server key, unique within `used`.
    Colliding slugs get a numeric suffix so two sources never overwrite each
    other's entry - which is also why a name shown in the admin UI is the base
    form: a run that selects two sources slugging alike numbers the second."""
    base = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "kb"
    name = base
    i = 2
    while used and name in used:
        name = f"{base}_{i}"
        i += 1
    return name


def kb_server_name(kb) -> str | None:
    """The MCP server key a knowledge base gets in a dev run, or None when it is
    a retrieval source with no callable tool."""
    if kb.kind in RETRIEVAL_KINDS:
        return None
    if kb.kind == "context7":
        return "context7"
    return server_name(kb.name)


def kb_tools(kb) -> list[str]:
    """The tool names under that server, when we ship the server ourselves."""
    return list(KNOWN_TOOLS.get(kb.kind, ()))


def tool_server_name(tool) -> str:
    """§Tools rows are already slugged; normalize through the same rule so the
    displayed name can never drift from the dispatcher's."""
    return server_name(tool.slug)
