"""Stray tool-call arguments (§stray arguments): repair a call the SDK would
otherwise reject, when - and only when - the repair is unambiguous.

Every OpenHands action schema is `extra="forbid"`, so a tool call carrying ONE
key the tool does not declare ends the step with a pydantic error the model has
to recover from: a full round-trip lost, and an "error" line on the customer's
console. On production runs the stray key is (nearly always) the model renaming
the SDK's own optional `summary` meta-field - `description`, `styles` - or its
`security_risk` (`security_skill`) on a call whose real arguments are complete:
dropping the key gives the exact call the model meant. The two guards keep the
repair honest: a key that ANOTHER tool in the roster declares may be a payload
sent to the wrong tool (`think` called with file_editor's `path`/`old_str`),
where executing the remainder would silently do something else - that one keeps
the SDK's error so the model re-targets; and the remainder must validate, or the
SDK's message stays intact (it names the real problem better than a stripped
dict would). No SDK import here: the driver passes the schema facts in, so the
rule is testable without the runner image."""

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
