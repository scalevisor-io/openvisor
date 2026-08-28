"""§stray arguments (runner/tool_args.py): a tool call carrying a key the
schema forbids is repaired only when the repair is unambiguous - the stray key
is nobody else's payload and the remainder validates - else the SDK's own error
stands. Loaded by file path from the compose.dev runner mount, leak-scan style."""
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
