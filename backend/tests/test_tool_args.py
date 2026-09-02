"""§tool-call repairs (runner/tool_args.py): a call the SDK would reject is
repaired only when the repair is unambiguous - a stray key is nobody else's
payload and the remainder validates, a wrong literal is the one the call
itself implies, a required-with-default schema field takes its default, a
prefixed name resolves to one roster tool - else the SDK's own error stands.
Loaded by file path from the compose.dev runner mount, leak-scan style."""
import importlib.util
import pathlib

import pytest

TOOL_ARGS = pathlib.Path("/app/runner_src/tool_args.py")

TERMINAL = {"command", "is_input", "timeout", "reset"}
FILE_EDITOR = {"command", "path", "file_text", "old_str", "new_str", "insert_line", "view_range"}
THINK = {"thought"}
TASK = {"description", "prompt", "subagent_type", "resume"}
ROSTER = TERMINAL | FILE_EDITOR | THINK | TASK


@pytest.fixture(scope="module")
def tool_args():
    if not TOOL_ARGS.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    spec = importlib.util.spec_from_file_location("tool_args_under_test", TOOL_ARGS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator(fields, required):
    def validate(candidate):
        extra = set(candidate) - fields
        missing = required - set(candidate)
        if extra or missing:
            raise ValueError(f"extra={extra} missing={missing}")
    return validate


TERMINAL_OK = _validator(TERMINAL, {"command"})


def test_stray_description_on_a_complete_terminal_call_is_dropped(tool_args):
    for value in ("", "Read blog meta and inspect illustration assets"):
        fixed, dropped = tool_args.repair(
            {"command": "ls app/src", "description": value}, TERMINAL, ROSTER, TERMINAL_OK)
        assert fixed == {"command": "ls app/src"}
        assert dropped == ["description"]


def test_unknown_keys_are_dropped_when_no_tool_declares_them(tool_args):
    fixed, dropped = tool_args.repair(
        {"command": "git status", "styles": "Read page wrapper conventions",
         "security_skill": "LOW"}, TERMINAL, ROSTER, TERMINAL_OK)
    assert fixed == {"command": "git status"}
    assert dropped == ["styles", "security_skill"]


def test_another_tools_payload_is_never_dropped(tool_args):
    # `think` called with file_editor's arguments: executing the remainder
    # would log a thought and silently lose the edit - the SDK's error stands.
    call = {"thought": "apply the edit", "path": "/workspace/a.py", "old_str": "x", "new_str": "y"}
    fixed, dropped = tool_args.repair(call, THINK, ROSTER, _validator(THINK, {"thought"}))
    assert fixed == call and dropped == []
    # `terminal` called with task's `prompt`: same rule, `description` alone
    # would have been droppable but `prompt` is a payload.
    call = {"command": "ls", "description": "explore", "prompt": "list the repo"}
    fixed, dropped = tool_args.repair(call, TERMINAL, ROSTER, TERMINAL_OK)
    assert fixed == call and dropped == []


def test_remainder_must_validate_or_the_call_stays_intact(tool_args):
    call = {"commit": "ls", "description": "run it"}  # command misspelt: still missing
    fixed, dropped = tool_args.repair(call, TERMINAL, ROSTER, TERMINAL_OK)
    assert fixed == call and dropped == []


def test_sdk_meta_keys_are_not_stray(tool_args):
    call = {"command": "ls", "summary": "list files", "security_risk": "LOW"}
    fixed, dropped = tool_args.repair(call, TERMINAL, ROSTER, TERMINAL_OK)
    assert fixed == call and dropped == []
    # ...and they are excluded from the validation probe, which the SDK runs
    # only after popping them.
    call = {"command": "ls", "summary": "list files", "description": ""}
    fixed, dropped = tool_args.repair(call, TERMINAL, ROSTER, TERMINAL_OK)
    assert fixed == {"command": "ls", "summary": "list files"} and dropped == ["description"]


def test_clean_calls_and_non_dicts_pass_through(tool_args):
    call = {"command": "ls"}
    assert tool_args.repair(call, TERMINAL, ROSTER, TERMINAL_OK) == (call, [])
    assert tool_args.repair("not json", TERMINAL, ROSTER, TERMINAL_OK) == ("not json", [])


FILE_EDITOR_COMMANDS = {"view", "create", "str_replace", "insert", "undo_edit"}
EDITOR_OK = _validator(FILE_EDITOR, {"command", "path"})


def test_wrong_literal_takes_the_reading_the_payload_implies(tool_args):
    fixed, rep = tool_args.repair_literal(
        {"command": "write", "path": "/w/a.md", "file_text": "hi"}, "command", FILE_EDITOR_COMMANDS, EDITOR_OK)
    assert rep == "create" and fixed["command"] == "create" and fixed["file_text"] == "hi"
    fixed, rep = tool_args.repair_literal(
        {"command": "edit", "path": "/w/a.md", "old_str": "a", "new_str": "b"}, "command", FILE_EDITOR_COMMANDS, EDITOR_OK)
    assert rep == "str_replace"
    # a synonym with no payload hint, and a bare path
    assert tool_args.repair_literal({"command": "undo", "path": "/w/a.md"}, "command", FILE_EDITOR_COMMANDS, EDITOR_OK)[1] == "undo_edit"
    assert tool_args.repair_literal({"command": "Read", "path": "/w/a.md"}, "command", FILE_EDITOR_COMMANDS, EDITOR_OK)[1] == "view"
    assert tool_args.repair_literal({"command": "Str-Replace", "path": "/w", "old_str": "a"}, "command", FILE_EDITOR_COMMANDS, EDITOR_OK)[1] == "str_replace"


def test_valid_or_unreadable_literals_stay_intact(tool_args):
    call = {"command": "view", "path": "/w/a.md"}
    assert tool_args.repair_literal(call, "command", FILE_EDITOR_COMMANDS, EDITOR_OK) == (call, None)
    call = {"command": "frobnicate", "file_text": "x"}  # no path: no reading validates
    assert tool_args.repair_literal(call, "command", FILE_EDITOR_COMMANDS, EDITOR_OK) == (call, None)
    assert tool_args.repair_literal({"command": 3, "path": "/w"}, "command", FILE_EDITOR_COMMANDS, EDITOR_OK)[1] is None


def test_required_with_default_schema_fields_are_filled(tool_args):
    schema = {"type": "object", "required": ["scale", "target"],
              "properties": {"scale": {"type": "string", "default": "css"}, "target": {"type": "string"},
                             "type": {"type": "string", "default": "png"}}}
    fixed, filled = tool_args.fill_schema_defaults({"type": "jpeg"}, schema)
    assert fixed == {"type": "jpeg", "scale": "css"} and filled == ["scale"]  # `target` has no default: left to the schema error
    call = {"type": "jpeg", "scale": "device"}
    assert tool_args.fill_schema_defaults(call, schema) == (call, [])
    assert tool_args.fill_schema_defaults(call, None) == (call, [])


def test_prefixed_or_miscased_tool_names_resolve_to_roster_names(tool_args):
    roster = {"terminal", "task_tracker", "browser_browser_navigate"}
    assert tool_args.repair_tool_name("tool_task_tracker", roster) == "task_tracker"
    assert tool_args.repair_tool_name("functions.terminal", roster) == "terminal"
    assert tool_args.repair_tool_name("Terminal", roster) == "terminal"
    assert tool_args.repair_tool_name("terminal", roster) is None  # already a roster name
    assert tool_args.repair_tool_name("tool_grep", roster) is None  # resolves to nothing
    assert tool_args.repair_tool_name("browser_browser_navigate", roster) is None


# ---- §14.5 per-command cap: default_timeout ----

def test_default_timeout_bounds_an_unbounded_terminal_call(tool_args):
    args, applied = tool_args.default_timeout(
        {"command": "docker compose up --build"}, "TerminalAction", TERMINAL, 600)
    assert applied and args["timeout"] == 600.0


def test_default_timeout_keeps_the_models_own_value(tool_args):
    args, applied = tool_args.default_timeout(
        {"command": "sleep 5", "timeout": 30}, "TerminalAction", TERMINAL, 600)
    assert not applied and args["timeout"] == 30


def test_default_timeout_ignores_non_terminal_actions(tool_args):
    # file_editor also has a `command` field - the terminal SHAPE is what matches
    args, applied = tool_args.default_timeout(
        {"command": "view", "path": "/x"}, "FileEditorAction", FILE_EDITOR, 600)
    assert not applied and "timeout" not in args


def test_default_timeout_matches_on_shape_when_the_name_is_unfamiliar(tool_args):
    args, applied = tool_args.default_timeout(
        {"command": "ls"}, "RenamedShellAction", TERMINAL, 60)
    assert applied and args["timeout"] == 60.0


def test_default_timeout_is_off_at_zero_and_never_raises(tool_args):
    assert tool_args.default_timeout({"command": "ls"}, "TerminalAction", TERMINAL, 0) == (
        {"command": "ls"}, False)
    garbage = object()
    assert tool_args.default_timeout(garbage, "TerminalAction", TERMINAL, 600) == (garbage, False)
    assert tool_args.default_timeout({"is_input": True}, "TerminalAction", TERMINAL, 600)[1] is False
