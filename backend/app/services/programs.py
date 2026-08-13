"""Program (§28) domain logic shared by the API and the worker: input-template
parsing and deterministic input validation, schedule (cron) validation, model
config resolution, run-workspace paths, and the env file the sandbox sources.

The input contract is `/input.template.yml` at the program repo root:

    inputs:
      - name: news_source_url        # shell-identifier-style, unique
        label: News source URL       # optional, defaults to name
        description: What to ingest  # optional
        type: text                   # text|multiline|number|boolean|choice
        required: true               # optional, default false
        default: ""                  # optional scalar; used when value omitted
        secret: false                # optional; masks the value in the UI
        placeholder: https://...     # optional UI hint
        options: [a, b]              # choice type only

The customer-facing form is generated from this - validation is deterministic
(no LLM step) so a wrong input names exactly which field failed and why.
"""
import ipaddress
import json
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from croniter import croniter

from app.core.config import settings
from app.core.encryption import decrypt

TEMPLATE_FILE = "input.template.yml"
INPUT_TYPES = ("text", "multiline", "number", "boolean", "choice")
_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_UUIDISH_RE = re.compile(r"^[0-9a-fA-F-]{1,64}$")


class TemplateError(ValueError):
    """input.template.yml is malformed - the message is shown to the admin."""


def parse_input_template(text: str) -> list[dict]:
    """Parse and strictly validate the template. Returns the normalized field
    list (cached on Program.input_template and served to the SPA form)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TemplateError(f"{TEMPLATE_FILE} is not valid YAML: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("inputs"), list):
        raise TemplateError(f"{TEMPLATE_FILE} must contain a top-level `inputs:` list")
    fields: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(data["inputs"]):
        where = f"inputs[{i}]"
        if not isinstance(raw, dict):
            raise TemplateError(f"{where} must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise TemplateError(f"{where}.name must be an identifier ([a-zA-Z_][a-zA-Z0-9_]*, max 64)")
        if name in seen:
            raise TemplateError(f"duplicate input name '{name}'")
        seen.add(name)
        ftype = raw.get("type", "text")
        if ftype not in INPUT_TYPES:
            raise TemplateError(f"{where}.type '{ftype}' is not one of {', '.join(INPUT_TYPES)}")
        options = raw.get("options")
        if ftype == "choice" and (not isinstance(options, list) or not options
                                  or not all(isinstance(o, (str, int, float)) for o in options)):
            raise TemplateError(f"{where}: choice inputs need a non-empty `options` list")
        default = raw.get("default")
        if default is not None and not isinstance(default, (str, int, float, bool)):
            raise TemplateError(f"{where}.default must be a scalar")
        fields.append({
            "name": name,
            "label": str(raw.get("label") or name),
            "description": str(raw.get("description") or ""),
            "type": ftype,
            "required": bool(raw.get("required", False)),
            "default": default,
            "secret": bool(raw.get("secret", False)),
            "placeholder": str(raw.get("placeholder") or ""),
            "options": [str(o) for o in options] if ftype == "choice" else None,
        })
    return fields


def validate_inputs(fields: list[dict], values: dict) -> tuple[dict[str, str], dict]:
    """Deterministic input validation against the template. Returns
    (errors, resolved): per-field error messages (empty = valid) and the
    resolved values - defaults filled, numbers/booleans coerced - ready to be
    dumped as input/input.yml. Unknown keys are errors so a renamed template
    field can't silently drop a customer's value."""
    errors: dict[str, str] = {}
    resolved: dict = {}
    known = {f["name"] for f in fields}
    for key in values:
        if key not in known:
            errors[key] = f"unknown input (not declared in {TEMPLATE_FILE})"
    for f in fields:
        name = f["name"]
        raw = values.get(name)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            if f["default"] is not None:
                resolved[name] = f["default"]
            elif f["required"]:
                errors[name] = "required input is missing"
            continue
        if f["type"] == "number":
            try:
                num = float(raw)
            except (TypeError, ValueError):
                errors[name] = "must be a number"
                continue
            resolved[name] = int(num) if num.is_integer() else num
        elif f["type"] == "boolean":
            text = str(raw).strip().lower()
            if isinstance(raw, bool):
                resolved[name] = raw
            elif text in ("true", "1", "yes", "on"):
                resolved[name] = True
            elif text in ("false", "0", "no", "off"):
                resolved[name] = False
            else:
                errors[name] = "must be true or false"
        elif f["type"] == "choice":
            if str(raw) not in (f["options"] or []):
                errors[name] = "not one of: " + ", ".join(f["options"] or [])
            else:
                resolved[name] = str(raw)
        else:  # text | multiline
            resolved[name] = str(raw)
    return errors, resolved


def load_inputs(instance) -> dict:
    """Decrypt an instance's stored input values ({name: value}). Values are
    envelope-encrypted at rest as one JSON blob (Memory parity: returned in
    clear by the API, `secret` template fields drive display masking only)."""
    if not instance.inputs_enc:
        return {}
    try:
        data = json.loads(decrypt(instance.inputs_enc))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def validate_cron(expr: str) -> str | None:
    """None when the expression is valid AND respects the schedule floor
    (PROGRAM_MIN_SCHEDULE_MINUTES); else a human-readable error. The floor is
    checked by sampling successive occurrences - exact enough for real crons
    without solving the general minimum-gap problem."""
    expr = (expr or "").strip()
    if not expr:
        return "cron expression is empty"
    try:
        it = croniter(expr, datetime.now(timezone.utc))
    except (ValueError, KeyError) as exc:
        return f"invalid cron expression: {exc}"
    floor_s = settings.program_min_schedule_minutes * 60 - 30  # slack for uneven crons
    prev = it.get_next(datetime)
    for _ in range(8):
        nxt = it.get_next(datetime)
        if (nxt - prev).total_seconds() < floor_s:
            return (f"schedule runs more often than every "
                    f"{settings.program_min_schedule_minutes} minutes (platform floor)")
        prev = nxt
    return None


def next_run(expr: str, after: datetime | None = None) -> datetime:
    return croniter(expr, after or datetime.now(timezone.utc)).get_next(datetime)


def validate_webhook_url(url: str) -> str | None:
    """None when acceptable ("" clears the webhook). http(s) only; outside
    local dev the host must not resolve into private/loopback ranges - the
    worker POSTs run results from inside the platform network, so a customer
    URL like http://deployer:8500/... would be SSRF. DNS-rebinding is out of
    scope (defence in depth; internal services also validate their inputs).
    Blocking call (getaddrinfo) - run it in a threadpool from async handlers."""
    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "webhook URL must be http(s)://host/..."
    if settings.is_local:
        return None
    try:
        infos = socket.getaddrinfo(parsed.hostname,
                                   parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return "webhook host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return "webhook host resolves to a private address"
    return None


def model_endpoint_ids(program, instance=None) -> list[str]:
    """Saved-endpoint ids this run could resolve through, in precedence order:
    the customer's per-instance choice first, then the admin's per-program
    default. Pure, so the async API and the sync worker both use it before
    fetching rows. Check runs pass instance=None and get the program default."""
    ids = [instance.model_endpoint_id if instance is not None else None,
           program.model_endpoint_id]
    return [i for i in ids if i]


def resolve_model_config(db, program, instance=None) -> tuple[str, str, str]:
    """(base_url, api_key, model) handed to the sandbox as OPENAI_*. A saved
    ModelEndpoint wins so its credential rotates in one place - the instance's
    own pick (§28 per-instance model) before the program's admin-set default;
    the legacy inline openai_* columns still resolve for programs configured
    before saved endpoints; else the global settings."""
    from app.models import ModelEndpoint
    for endpoint_id in model_endpoint_ids(program, instance):
        ep = db.get(ModelEndpoint, endpoint_id)
        if ep is not None and ep.model_name:
            return ep.base_url, decrypt(ep.api_key_enc), ep.model_name
    base = program.openai_base_url or settings.openai_base_url
    key = decrypt(program.openai_api_key_enc) if program.openai_api_key_enc else settings.openai_api_key
    model = program.model_name or settings.openai_model
    return base, key, model


def program_model_keys(program, endpoints) -> list[str]:
    """Every model credential this run could hand to the sandbox - the saved
    endpoints it may resolve through (the instance pick AND the program default,
    since a model-less row makes resolution fall through) plus the legacy inline
    key - for the leak-scan refuse set and log redaction. A superset on purpose:
    a key in the set but unused costs nothing, a used key missing from it is a
    leak. The global platform key is already covered by
    leakscan.platform_secret_values. Pure - callers fetch the rows themselves
    (sync worker / async API)."""
    keys = [decrypt(ep.api_key_enc) for ep in endpoints
            if ep is not None and ep.api_key_enc]
    if program.openai_api_key_enc:
        keys.append(decrypt(program.openai_api_key_enc))
    return keys


def sandbox_name(program_id: str, instance_id: str | None) -> str:
    """DinD container/pod name: one per instance (runs are serialized on it and
    share its docker layer-cache volume), a program-scoped one for admin checks."""
    return f"prog-{instance_id}" if instance_id else f"progchk-{program_id}"


def _instance_dir_name(program_id: str, instance_id: str | None) -> str:
    name = instance_id or f"check-{program_id}"
    if not _UUIDISH_RE.match(name.removeprefix("check-")):
        raise ValueError(f"unsafe instance dir name: {name}")
    return name


def rel_run_dir(program_id: str, instance_id: str | None, run_id: str) -> str:
    """A run's artifact dir relative to the workspaces root - what the deployer
    receives (it re-anchors the path under its own /workspaces mount). Holds
    run.log (streamed live), output/ (copied out of the sandbox), usage.json
    (billing report) and, while the run executes, work/ (the staged sandbox
    content: repo + input/ + secrets/ + .openvisor/)."""
    if not _UUIDISH_RE.match(run_id):
        raise ValueError(f"unsafe run id: {run_id}")
    return f"programs/{_instance_dir_name(program_id, instance_id)}/runs/{run_id}"


def run_dir(program_id: str, instance_id: str | None, run_id: str) -> Path:
    return Path(settings.workspaces_dir) / rel_run_dir(program_id, instance_id, run_id)


def instance_dir(program_id: str, instance_id: str | None) -> Path:
    return (Path(settings.workspaces_dir) / "programs"
            / _instance_dir_name(program_id, instance_id))


def write_program_env(db, work: Path, program, instance=None) -> None:
    """Write work/.openvisor/program.env - the env the deployer SOURCES inside the
    sandbox before every compose phase. Never passed as `docker exec -e`: values
    must not reach command lines, deployer logs or `docker inspect` (same trust
    boundary as the dev runner's secrets.env). Carries the platform model vars
    (PROMPT §28, resolved for THIS instance) plus the resource knobs the template
    compose file interpolates."""
    base_url, api_key, model = resolve_model_config(db, program, instance)
    pairs = [
        ("OPENAI_BASE_URL", base_url),
        ("OPENAI_API_KEY", api_key),
        ("OPENAI_MODEL", model),
        ("EMBEDDING_BASE_URL", settings.embedding_base_url),
        ("EMBEDDING_API_KEY", settings.embedding_api_key),
        ("EMBEDDING_MODEL", settings.embedding_model),
        ("CONTEXT7_MCP_URL", settings.context7_mcp_url),
        ("CONTEXT7_API_KEY", settings.context7_api_key),
        ("PROGRAM_CPUS", program.cpu_limit),
        ("PROGRAM_MEM_LIMIT", program.mem_limit),
        ("PROGRAM_MEM_RESERVATION", program.mem_request),
    ]
    openvisor = work / ".openvisor"
    openvisor.mkdir(parents=True, exist_ok=True)
    env_path = openvisor / "program.env"
    env_path.write_text("\n".join(
        f"{name}='" + str(value or "").replace("'", "'\\''") + "'"
        for name, value in pairs) + "\n")
    env_path.chmod(0o600)


def prune_runs(inst_dir: Path, keep: int) -> None:
    """Delete the oldest run artifact dirs beyond the retention count. DB run
    rows keep the log tail and output text, so history stays readable - only
    per-file downloads of pruned runs 404."""
    runs = inst_dir / "runs"
    if not runs.is_dir() or keep <= 0:
        return
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime)
    for d in dirs[:-keep]:
        shutil.rmtree(d, ignore_errors=True)
