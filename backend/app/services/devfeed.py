"""Live dev-run activity feed (§14.8). The runner (runner/live_events.py +
entrypoint phase markers) and the worker (its own phases: state changes, boot
gate, metering) append small sanitized JSON events to
<workspace>/.openvisor/events.jsonl, and the runner snapshots token usage to
progress.json; `GET /projects/{id}/dev-activity?offset=` offset-polls both
straight off the shared workspaces volume (Programs §28 log parity).

The feed carries only summaries composed by our own code - never raw model
output, task text, or command output - and every served chunk gets a second
defensive pass: platform-secret values are redacted, and a line carrying
private-key material or a verbatim knowledge-base span (the run's
.openvisor/leak_kb.json fingerprints, leak-scan parity) is withheld outright."""
import json
import time
from pathlib import Path

from app.core.encryption import decrypt
from app.services import leakscan

CHUNK_BYTES = 65536
LIVE_STATES = ("running", "deploying")
WITHHELD = "[withheld - confidential content]"


def _openvisor_dir(project) -> Path:
    from app.services.dev_concurrency import run_ws
    return run_ws(project) / ".openvisor"


def feed_path(project) -> Path:
    return _openvisor_dir(project) / "events.jsonl"


def progress_path(project) -> Path:
    return _openvisor_dir(project) / "progress.json"


def reset(project) -> None:
    """Start-of-run cleanup: a new run must not replay the previous feed (the
    SPA restarts its offset via the chunk's shrunken next_offset / reset flag)."""
    try:
        feed_path(project).unlink(missing_ok=True)
        progress_path(project).unlink(missing_ok=True)
    except OSError:
        pass


def append_event(project, kind: str, title: str, detail: str | None = None) -> None:
    """Worker-side feed writer (runner events come from runner/live_events.py).
    Best-effort by contract: the feed must never fail a build."""
    try:
        d = _openvisor_dir(project)
        d.mkdir(parents=True, exist_ok=True)
        ev: dict = {"ts": round(time.time(), 3), "kind": kind, "title": title[:200]}
        if detail:
            ev["detail"] = detail[:400]
        with feed_path(project).open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _guards(project) -> tuple[list[str], list[str]]:
    """(secret values to redact, KB fingerprints to withhold on) for a served
    chunk. Best-effort - serving the feed beats a perfect guard here; the
    runner-side summarizer and leak scan remain the primary controls."""
    secrets: list[str] = []
    fingerprints: list[str] = []
    try:
        keys = [decrypt(project.ssh_private_key_enc)] if project.ssh_private_key_enc else []
        secrets = leakscan.platform_secret_values(ssh_private_keys=keys)
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = json.loads((_openvisor_dir(project) / "leak_kb.json").read_text())
        fingerprints = [f for f in raw if isinstance(f, str)]
    except Exception:  # noqa: BLE001
        pass
    return secrets, fingerprints


def _clean_text(text: str, secrets: list[str], fingerprints: list[str]) -> str:
    if leakscan.PRIVATE_KEY_RE.search(text):
        return WITHHELD
    for v in secrets:
        text = text.replace(v, "[redacted]")
    if fingerprints:
        norm = leakscan.norm_ws(text)
        if any(fp in norm for fp in fingerprints):
            return WITHHELD
    return text


def read_chunk(project, offset: int, endpoint_prices: dict | None = None) -> dict:
    """One offset-polled chunk of the activity feed plus the live usage
    snapshot. Only complete JSONL lines are consumed (a partial tail line
    stays for the next poll); a missing or shrunken file (new run) hands the
    client a smaller next_offset / reset flag so it restarts its buffer.
    `endpoint_prices` = {api_model: (input, output, cached_input)} from the
    caller's session - see read_progress."""
    from app.services.dev_concurrency import bound_run
    # §parallel-builds MR4: a run-scoped read reports ITS row's state and clock,
    # not the mirror's - a sibling console must not show the primary's phase.
    # Unbound (or a bound row the mirror tracks) keeps today's values.
    row = bound_run(project)
    state = (row.state if row is not None and row.state else project.dev_run_state)
    out = {"state": state, "live": state in LIVE_STATES,
           "started_at": (row.started_at if row is not None and row.started_at
                          else project.dev_run_started_at),
           "events": [], "next_offset": max(0, offset), "reset": False,
           "usage": read_progress(project, endpoint_prices)}
    path = feed_path(project)
    try:
        size = path.stat().st_size
    except OSError:
        out["next_offset"] = 0
        return out
    offset = max(0, offset)
    if offset > size:  # replaced by a newer run - restart the client buffer
        offset, out["reset"] = 0, True
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(CHUNK_BYTES)
    cut = data.rfind(b"\n")
    if cut < 0:
        out["next_offset"] = offset
        return out
    secrets, fingerprints = _guards(project)
    events = []
    for line in data[:cut].splitlines():
        try:
            ev = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue  # torn/foreign line (e.g. first read landing mid-line)
        if not isinstance(ev, dict):
            continue
        slim = {"ts": ev.get("ts"),
                "kind": str(ev.get("kind") or "info")[:24],
                "title": _clean_text(str(ev.get("title") or ""), secrets, fingerprints)[:300]}
        detail = ev.get("detail")
        if detail:
            slim["detail"] = _clean_text(str(detail), secrets, fingerprints)[:600]
        events.append(slim)
    out["events"] = events
    out["next_offset"] = offset + cut + 1
    return out


def read_progress(project, endpoint_prices: dict | None = None) -> dict | None:
    """The runner's live token snapshot plus a display-only credit estimate at
    the platform markup. Billing is untouched: it still happens once per
    dispatch, from usage.json, in the worker.

    `endpoint_prices` maps api_model -> the admin-supplied (input, output,
    cached_input) per-1M tuple of a saved ModelEndpoint. The caller passes it
    because this runs in a threadpool with no session; without it a project
    routed to a model that is priced ONLY by its endpoint (not by the static
    table) showed no estimate at all while billing it fine."""
    try:
        raw = json.loads(progress_path(project).read_text())
        usage = {"input_tokens": int(raw.get("input_tokens") or 0),
                 "output_tokens": int(raw.get("output_tokens") or 0),
                 "cached_input_tokens": int(raw.get("cached_input_tokens") or 0)}
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        # The api_model this run executes on - the console names it per run
        # because the project's routing can change between runs.
        usage["model"] = str(raw.get("model") or "") or None
    except Exception:  # noqa: BLE001 - absent/torn snapshot = no live counter
        return None
    usage["credits_estimate"] = None
    try:
        from app.services.pricing import cost_credits
        model = raw.get("model")
        if model:
            usage["credits_estimate"] = round(
                cost_credits(model, usage["input_tokens"], usage["output_tokens"],
                             price=(endpoint_prices or {}).get(model),
                             cached_input_tokens=usage["cached_input_tokens"]), 4)
    except Exception:  # noqa: BLE001 - unpriced model: show tokens, no estimate
        pass
    return usage
