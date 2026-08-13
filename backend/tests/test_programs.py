"""Programs (§28) pure-logic tests: input-template parsing/validation, cron
floor, per-program billing markup, sandbox env file, output leak scan, webhook
delivery. No DB - DB-backed cases live in test_programs_db.py."""
import pytest

from app.models import Program, ProgramInstance, ProgramRun
from app.services import leakscan
from app.services import programs as programs_svc
from app.services.pricing import UnknownModelError, cost_credits
from app.core.config import settings


# ---- input.template.yml parsing ----

VALID_TEMPLATE = """
inputs:
  - name: news_source_url
    label: News source URL
    description: What to ingest
    type: text
    required: true
    placeholder: https://example.com/feed
  - name: max_items
    type: number
    default: 10
  - name: tone
    type: choice
    options: [friendly, formal]
    default: friendly
  - name: dry_run
    type: boolean
  - name: api_secret
    type: text
    secret: true
"""


def test_parse_input_template_normalizes_fields():
    fields = programs_svc.parse_input_template(VALID_TEMPLATE)
    assert [f["name"] for f in fields] == [
        "news_source_url", "max_items", "tone", "dry_run", "api_secret"]
    url = fields[0]
    assert url["label"] == "News source URL"
    assert url["required"] is True and url["type"] == "text" and url["options"] is None
    assert fields[1]["default"] == 10
    assert fields[2]["options"] == ["friendly", "formal"]
    assert fields[4]["secret"] is True


@pytest.mark.parametrize("text,fragment", [
    ("inputs: {a: 1}", "top-level `inputs:` list"),
    ("nope: []", "top-level `inputs:` list"),
    ("inputs:\n  - name: 'bad name!'", "identifier"),
    ("inputs:\n  - name: ok\n    type: image", "not one of"),
    ("inputs:\n  - name: c\n    type: choice", "options"),
    ("inputs:\n  - name: dup\n  - name: dup", "duplicate"),
    ("inputs:\n  - name: d\n    default: [1, 2]", "scalar"),
    ("inputs: [\n", "not valid YAML"),
])
def test_parse_input_template_rejects_bad_templates(text, fragment):
    with pytest.raises(programs_svc.TemplateError) as exc:
        programs_svc.parse_input_template(text)
    assert fragment in str(exc.value)


def test_validate_inputs_names_each_failing_field():
    fields = programs_svc.parse_input_template(VALID_TEMPLATE)
    errors, resolved = programs_svc.validate_inputs(fields, {
        "max_items": "not-a-number",
        "tone": "sarcastic",
        "dry_run": "maybe",
        "ghost": "x",
    })
    assert errors["news_source_url"] == "required input is missing"
    assert errors["max_items"] == "must be a number"
    assert "not one of: friendly, formal" in errors["tone"]
    assert errors["dry_run"] == "must be true or false"
    assert "unknown input" in errors["ghost"]
    assert resolved == {}  # nothing valid to resolve except... all failed


def test_validate_inputs_coerces_and_fills_defaults():
    fields = programs_svc.parse_input_template(VALID_TEMPLATE)
    errors, resolved = programs_svc.validate_inputs(fields, {
        "news_source_url": "https://example.com/feed",
        "max_items": "25",
        "dry_run": "Yes",
    })
    assert errors == {}
    assert resolved == {
        "news_source_url": "https://example.com/feed",
        "max_items": 25,          # coerced to int
        "tone": "friendly",       # default filled
        "dry_run": True,          # boolean parsed
    }
    # empty string counts as unset → required error
    errors2, _ = programs_svc.validate_inputs(fields, {"news_source_url": "   "})
    assert "news_source_url" in errors2


# ---- cron validation ----

def test_validate_cron_rejects_syntax_and_floor():
    assert programs_svc.validate_cron("") is not None
    assert "invalid cron" in programs_svc.validate_cron("not a cron")
    floor = settings.program_min_schedule_minutes
    assert f"every {floor} minutes" in programs_svc.validate_cron("* * * * *")
    assert programs_svc.validate_cron("*/5 * * * *") is not None
    assert programs_svc.validate_cron(f"*/{floor} * * * *") is None
    assert programs_svc.validate_cron("0 * * * *") is None
    assert programs_svc.validate_cron("0 7 * * 1") is None


def test_next_run_is_future_and_tz_aware():
    from app.models import utcnow
    now = utcnow()
    nxt = programs_svc.next_run("0 * * * *", now)
    assert nxt > now and nxt.tzinfo is not None


# ---- billing markup ----

def test_cost_credits_markup_override_replaces_global():
    base = cost_credits("mistral-large-latest", 1_000_000, 0, markup=1.0)
    assert cost_credits("mistral-large-latest", 1_000_000, 0, markup=2.0) == pytest.approx(base * 2)
    assert cost_credits("mistral-large-latest", 1_000_000, 0) == pytest.approx(
        base * settings.credit_markup)
    with pytest.raises(UnknownModelError):
        cost_credits("made-up-model", 10, 10, markup=2.0)


# ---- sandbox env file ----

def test_write_program_env_quotes_values_and_restricts_perms(tmp_path):
    program = Program(title="T", gitlab_repo_path="g/t", model_name="it's-a-model",
                      cpu_limit="1.5", mem_limit="2g", mem_request="512m")
    programs_svc.write_program_env(None, tmp_path, program)
    env_path = tmp_path / ".openvisor" / "program.env"
    assert env_path.stat().st_mode & 0o777 == 0o600
    text = env_path.read_text()
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
                 "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL",
                 "CONTEXT7_MCP_URL", "CONTEXT7_API_KEY",
                 "PROGRAM_CPUS", "PROGRAM_MEM_LIMIT", "PROGRAM_MEM_RESERVATION"):
        assert f"{name}='" in text
    # single quotes in values are shell-escaped, never break the quoting
    assert "OPENAI_MODEL='it'\\''s-a-model'" in text
    assert "PROGRAM_CPUS='1.5'" in text
    assert "PROGRAM_MEM_RESERVATION='512m'" in text


# ---- output leak scan ----

def test_scan_output_flags_secrets_and_kb_without_echoing_values(tmp_path):
    secret = "sk-super-secret-value-123456"
    kb_text = ("the quick brown fox jumps over the lazy dog " * 10).strip()
    fps = leakscan.kb_fingerprints([kb_text], "")
    assert fps

    out = tmp_path / "output"
    out.mkdir()
    (out / "leak.txt").write_text(f"header {secret} trailer")
    (out / "kb.txt").write_text(f"prefix {kb_text} suffix")
    (out / "clean.txt").write_text("nothing to see here")
    (out / "blob.bin").write_bytes(b"\x00\x01" + secret.encode())  # binary skipped

    files = [out / n for n in ("leak.txt", "kb.txt", "clean.txt", "blob.bin")]
    findings = leakscan.scan_output(tmp_path, files, "log also holds " + secret,
                                    [secret], fps)
    joined = "\n".join(findings)
    assert "output/leak.txt: contains a secret value" in joined
    assert "output/kb.txt: contains verbatim knowledge-base text" in joined
    assert "run log: contains a secret value" in joined
    assert "clean.txt" not in joined and "blob.bin" not in joined
    assert secret not in joined  # never echo the value

    assert leakscan.scan_output(tmp_path, [out / "clean.txt"], "clean log",
                                [secret], fps) == []


def test_scan_output_blocks_private_key_material_by_pattern(tmp_path):
    # value-independent: NOT one of the known secret values, still blocked
    out = tmp_path / "output"
    out.mkdir()
    (out / "some_key").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA...\n"
        "-----END OPENSSH PRIVATE KEY-----\n")
    (out / "rsa.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n")
    (out / "pub_ok.txt").write_text(
        "-----BEGIN PUBLIC KEY-----\nMFkw...\n-----END PUBLIC KEY-----\n")

    findings = leakscan.scan_output(
        tmp_path, [out / n for n in ("some_key", "rsa.pem", "pub_ok.txt")],
        "log carries -----BEGIN EC PRIVATE KEY----- too", [], [])
    joined = "\n".join(findings)
    assert "output/some_key: contains private-key material" in joined
    assert "output/rsa.pem: contains private-key material" in joined
    assert "run log: contains private-key material" in joined
    assert "pub_ok.txt" not in joined  # public keys are fine


def test_platform_secret_values_includes_key_material_not_pem_markers():
    pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2g\n"
           "-----END OPENSSH PRIVATE KEY-----\n")
    vals = leakscan.platform_secret_values(extra_values=["extra-secret-value"],
                                           ssh_private_keys=[pem])
    assert "extra-secret-value" in vals
    assert any(v.startswith("b3BlbnNzaC1rZXktdjE") for v in vals)
    assert not any("PRIVATE KEY" in v for v in vals)
    assert all(len(v) >= leakscan.MIN_SECRET_LEN for v in vals)


# ---- webhook delivery ----

class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def _bare_run_instance_program(url="https://hooks.example.com/x"):
    program = Program(id="p1", title="Prog", gitlab_repo_path="g/p1")
    inst = ProgramInstance(id="i1", program_id="p1", org_id="o1", label="lbl",
                           webhook_url=url, ssh_public_key="pk",
                           ssh_private_key_enc="enc")
    run = ProgramRun(id="r1", program_id="p1", instance_id="i1", org_id="o1",
                     kind="manual", state="succeeded", exit_code="0",
                     output_text="hello", cost_credits=0.5)
    return run, inst, program


def test_fire_webhook_delivers_payload_first_try(monkeypatch):
    from app.workers import programs as wp
    calls = []
    monkeypatch.setattr(wp.httpx, "post",
                        lambda url, json, timeout: calls.append((url, json)) or _Resp(200))
    monkeypatch.setattr(wp.time, "sleep", lambda s: pytest.fail("no retry expected"))
    run, inst, program = _bare_run_instance_program()
    wp._fire_webhook(run, program, inst)
    assert run.webhook_status == "delivered"
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == inst.webhook_url
    assert payload["run_id"] == "r1" and payload["state"] == "succeeded"
    assert payload["output"] == "hello"
    assert payload["program"] == {"id": "p1", "title": "Prog"}
    assert payload["credits_charged"] == 0.5


def test_fire_webhook_retries_then_fails(monkeypatch):
    from app.workers import programs as wp
    attempts = []
    monkeypatch.setattr(wp.httpx, "post",
                        lambda url, json, timeout: attempts.append(1) or _Resp(500))
    monkeypatch.setattr(wp.time, "sleep", lambda s: None)
    run, inst, program = _bare_run_instance_program()
    wp._fire_webhook(run, program, inst)
    assert len(attempts) == 3
    assert run.webhook_status == "failed"


def test_fire_webhook_skips_without_url(monkeypatch):
    from app.workers import programs as wp
    monkeypatch.setattr(wp.httpx, "post", lambda *a, **k: pytest.fail("must not POST"))
    run, inst, program = _bare_run_instance_program(url="")
    wp._fire_webhook(run, program, inst)
    assert run.webhook_status is None
    wp._fire_webhook(run, program, None)  # check runs have no instance


# ---- webhook URL validation (local mode allows private hosts) ----

def test_validate_webhook_url_scheme_gate():
    assert programs_svc.validate_webhook_url("") is None
    assert programs_svc.validate_webhook_url("ftp://x") is not None
    assert programs_svc.validate_webhook_url("not-a-url") is not None
    assert programs_svc.validate_webhook_url("https://example.com/hook") is None
