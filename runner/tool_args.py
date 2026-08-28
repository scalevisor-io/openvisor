"""Tool-call repairs (§tool-call repairs): make a call the SDK would otherwise
reject run, when - and only when - the repair is unambiguous.

Every OpenHands action schema is `extra="forbid"` and validates before the
tool sees the call, so one malformed detail ends the step with a pydantic
error the model has to recover from: a full round-trip lost, and an "error"
line on the customer's console. Four shapes, all read off production feeds:

- a STRAY KEY - the model renaming the SDK's own optional `summary` meta-field
  (`description`, `styles`) or its `security_risk` (`security_skill`) on a
  `terminal` call whose real arguments are complete (`repair`);
- a WRONG LITERAL - `file_editor` called with `command="write"` and a
  `file_text`, where the call itself names the command it meant
  (`repair_literal`);
- a REQUIRED-WITH-DEFAULT schema field - an MCP server declaring `scale`
  required AND defaulted (`browser_take_screenshot`), which the SDK's
  schema-to-model turns into a hard requirement the server itself never had
  (`fill_schema_defaults`);
- a PREFIXED tool name - `tool_task_tracker` for `task_tracker`
  (`repair_tool_name`).

Two guards keep every repair honest: a stray key that ANOTHER roster tool
declares may be a payload sent to the wrong tool (`think` called with
file_editor's `path`/`old_str`), where executing the remainder would silently
do something else - that one keeps the SDK's error so the model re-targets;
and the repaired call must validate, or the SDK's message stays intact (it
names the real problem better than a stripped dict would). No SDK import
here: the driver passes the schema facts in, so the rules are testable
without the runner image."""

# The SDK reads these off the call itself (agent._extract_summary /
# _extract_security_risk) before validating the action: never stray.
META_KEYS = frozenset({"summary", "security_risk"})
# Roster fields that are themselves a summary: `task.description` ("a short 3-5
# word description") is what a model reaching for `summary` writes on any tool.
SUMMARY_TWINS = frozenset({"description"})


def repair(arguments, fields, roster_fields, validate):
    """Return `(arguments, dropped)`: the call with its droppable stray keys
    removed and the list of what was dropped, or the input untouched and `[]`.

    `fields`: the target action's own field names (plus aliases).
    `roster_fields`: every field name declared by any tool in the roster.
    `validate(candidate)`: raises when `candidate` does not fit the schema."""
    if not isinstance(arguments, dict):
        return arguments, []
    stray = [k for k in arguments if k not in fields and k not in META_KEYS]
    if not stray:
        return arguments, []
    if any(k in roster_fields and k not in SUMMARY_TWINS for k in stray):
        return arguments, []
    kept = {k: v for k, v in arguments.items() if k not in stray}
    try:
        validate({k: v for k, v in kept.items() if k not in META_KEYS})
    except Exception:  # noqa: BLE001 - not a stray-key problem: the SDK's error stands
        return arguments, []
    return kept, stray


# The command a file_editor-style call implies by what it carries - the SDK's
# own inference order (`_infer_file_editor_command`), which the SDK applies
# only when `command` is ABSENT; a wrong value gets the same reading here.
PAYLOAD_HINTS = (("old_str", "str_replace"), ("insert_line", "insert"),
                 ("file_text", "create"))
LITERAL_SYNONYMS = {
    "write": "create", "new": "create", "add": "create",
    "read": "view", "cat": "view", "open": "view", "show": "view", "list": "view",
    "replace": "str_replace", "edit": "str_replace", "update": "str_replace",
    "modify": "str_replace", "patch": "str_replace",
    "undo": "undo_edit", "revert": "undo_edit",
}


def repair_literal(arguments, field, allowed, validate):
    """Return `(arguments, replacement)`: an invalid string for the literal
    `field` replaced by the first allowed reading that validates - its
    normalized spelling, what the payload implies, a synonym, `view` for a
    bare path - or the input untouched and `None`."""
    if not isinstance(arguments, dict):
        return arguments, None
    value = arguments.get(field)
    if not isinstance(value, str) or value in allowed:
        return arguments, None
    norm = value.strip().lower().replace("-", "_").replace(" ", "_")
    candidates = [norm] + [cmd for key, cmd in PAYLOAD_HINTS if key in arguments]
    candidates.append(LITERAL_SYNONYMS.get(norm))
    if "path" in arguments:
        candidates.append("view")
    seen = set()
    for cand in candidates:
        if cand is None or cand not in allowed or cand in seen:
            continue
        seen.add(cand)
        fixed = {**arguments, field: cand}
        try:
            validate({k: v for k, v in fixed.items() if k not in META_KEYS})
        except Exception:  # noqa: BLE001 - try the next reading
            continue
        return fixed, cand
    return arguments, None


def fill_schema_defaults(arguments, schema):
    """Return `(arguments, filled)`: every property the JSON schema lists as
    required AND gives a `default` - the server would apply it, the SDK's
    generated model demands it - filled with that default when absent."""
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        return arguments, []
    props = schema.get("properties") or {}
    filled = [name for name in (schema.get("required") or [])
              if name not in arguments and isinstance(props.get(name), dict)
              and "default" in props[name]]
    if not filled:
        return arguments, []
    return {**arguments, **{name: props[name]["default"] for name in filled}}, filled


# Wrappers models put around a tool name; stripped only when what remains IS
# a roster tool, so a real name is never rewritten.
TOOL_NAME_PREFIXES = ("tool_", "tools_", "tools.", "tool.", "functions.", "functions:",
                      "function.", "function_", "default_api.", "default_api:", "mcp__", "mcp_")


def repair_tool_name(name, available):
    """Return the roster name a mis-wrapped or mis-cased `name` denotes, or
    `None` when `name` is a roster name already or resolves to nothing."""
    if not isinstance(name, str) or name in available:
        return None
    for prefix in TOOL_NAME_PREFIXES:
        if name.startswith(prefix) and name[len(prefix):] in available:
            return name[len(prefix):]
    by_lower = {t.lower(): t for t in available}
    return by_lower.get(name.strip().lower())
