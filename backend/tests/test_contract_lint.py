"""Phase 1: the deterministic pre-boot demo-contract linter (services/contract.py).

Contract violations are the #1 harness failure class; this catches "no service
publishes $PORT" and unparseable compose in <1s before the DinD boot. It must be
HIGH-PRECISION (fail only when the contract is certainly violated) and fail OPEN
on anything unexpected, so it never rejects a valid build or breaks a run.
"""
from pathlib import Path

import pytest

from app.services.contract import check_demo_contract


def _write(d: Path, name: str, text: str) -> None:
    (d / name).write_text(text)


def test_valid_demo_with_port_passes(tmp_path):
    _write(tmp_path, "compose.demo.yml",
           'services:\n  web:\n    ports:\n      - "${PORT}:8080"\n')
    assert check_demo_contract(tmp_path) == (True, "")


def test_port_in_base_service_passes(tmp_path):
    # base defines the service+ports, demo overlays other settings - still valid.
    _write(tmp_path, "compose.base.yml",
           'services:\n  web:\n    build: ./site\n    ports:\n      - "${PORT}:8080"\n')
    _write(tmp_path, "compose.demo.yml",
           'services:\n  web:\n    restart: unless-stopped\n')
    ok, _ = check_demo_contract(tmp_path)
    assert ok is True


def test_long_form_published_port_passes(tmp_path):
    _write(tmp_path, "compose.demo.yml",
           "services:\n  web:\n    ports:\n      - target: 8080\n        published: ${PORT}\n")
    ok, _ = check_demo_contract(tmp_path)
    assert ok is True


def test_no_port_mapping_fails_with_actionable_message(tmp_path):
    _write(tmp_path, "compose.demo.yml",
           'services:\n  web:\n    ports:\n      - "8080:8080"\n')  # hardcoded, no $PORT
    ok, msg = check_demo_contract(tmp_path)
    assert ok is False
    assert "$PORT" in msg and "ports:" in msg


def test_lowercase_port_typo_fails(tmp_path):
    # ${port} would not substitute (compose vars are case-sensitive) -> must fail.
    _write(tmp_path, "compose.demo.yml",
           'services:\n  web:\n    ports:\n      - "${port}:8080"\n')
    assert check_demo_contract(tmp_path)[0] is False


def test_portfolio_does_not_false_match(tmp_path):
    # a bare word containing PORT must not count as publishing $PORT.
    _write(tmp_path, "compose.demo.yml",
           'services:\n  web:\n    environment:\n      - APP=$PORTFOLIO\n    ports:\n      - "3000:3000"\n')
    assert check_demo_contract(tmp_path)[0] is False


def test_network_mode_host_defers_to_boot_gate(tmp_path):
    # host netns binds $PORT with no `ports:` line - the real gate passes, so the
    # linter must NOT reject (would be a false positive blocking a good build).
    _write(tmp_path, "compose.demo.yml",
           "services:\n  web:\n    network_mode: host\n    environment:\n      - PORT\n")
    assert check_demo_contract(tmp_path) == (True, "")


def test_extends_defers_to_boot_gate(tmp_path):
    # ports inherited via extends from a file we don't read - must not reject.
    _write(tmp_path, "compose.demo.yml",
           "services:\n  web:\n    extends:\n      file: compose.common.yml\n      service: web\n")
    assert check_demo_contract(tmp_path) == (True, "")


def test_invalid_yaml_fails_closed_with_message(tmp_path):
    _write(tmp_path, "compose.demo.yml", "services:\n  web:\n  ports: [unbalanced\n")
    ok, msg = check_demo_contract(tmp_path)
    assert ok is False and "YAML" in msg


def test_missing_file_is_not_the_lints_job(tmp_path):
    # the boot gate owns the missing-compose.demo.yml message; the lint passes through.
    assert check_demo_contract(tmp_path) == (True, "")


def test_never_raises_on_garbage(tmp_path):
    # a compose that is valid yaml but a non-dict shape must fail open, not raise.
    _write(tmp_path, "compose.demo.yml", "- just\n- a\n- list\n")
    ok, _ = check_demo_contract(tmp_path)
    assert ok is True  # ambiguous/odd shape -> pass through to the boot gate
