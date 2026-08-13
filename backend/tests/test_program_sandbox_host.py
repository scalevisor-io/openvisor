"""§ssh remotes for programs: a program run's sandbox gets the SAME tailnet
git-host alias a dev-run sandbox gets.

A forge reachable only over the tailnet resolves to a CGNAT address no sandbox
can route to. Dev runs have always passed GIT_EXTRA_HOST to the deployer; the
program path did not, so a program cloning the customer's repositories hung
until git gave up with "Could not read from remote repository" - a failure that
looks like a bad deploy key and is not one. These pin the forwarding, because
the setting only exists if the sandbox actually receives it.
"""

from app.core.config import settings
from app.models import Program, ProgramInstance, ProgramRun
from app.services import deployer_client
from app.workers import programs as programs_worker


def test_run_program_puts_the_extra_host_in_the_body(monkeypatch):
    body = {}
    monkeypatch.setattr(deployer_client, "_call",
                        lambda m, p, json_body=None, timeout=0: body.update(json_body) or {})
    deployer_client.run_program("prog-1", "programs/i/runs/r", timeout_s=60,
                                cpu_limit="1", mem_limit="1g",
                                extra_host="git.example.com:10.0.0.5")
    assert body["extra_host"] == "git.example.com:10.0.0.5"


def test_run_program_omits_nothing_when_no_alias_is_configured(monkeypatch):
    """An empty setting must still send the key: the deployer defaults it to ""
    and a missing key would be indistinguishable from an older client."""
    body = {}
    monkeypatch.setattr(deployer_client, "_call",
                        lambda m, p, json_body=None, timeout=0: body.update(json_body) or {})
    deployer_client.run_program("prog-1", "programs/i/runs/r", timeout_s=60,
                                cpu_limit="1", mem_limit="1g")
    assert body["extra_host"] == ""


def test_execute_forwards_the_configured_tailnet_host(monkeypatch, tmp_path):
    """The dispatch path, not just the client: whatever GIT_EXTRA_HOST holds is
    what the program sandbox is created with."""
    sent = {}
    monkeypatch.setattr(settings, "git_extra_host", "git.acme.test:10.1.2.3")
    monkeypatch.setattr(settings, "workspaces_dir", str(tmp_path))
    monkeypatch.setattr(programs_worker, "_prepare", lambda *a, **k: True)
    monkeypatch.setattr(programs_worker, "_bill", lambda *a, **k: None)
    monkeypatch.setattr(programs_worker, "_finalize_outputs", lambda *a, **k: None)
    monkeypatch.setattr(programs_worker.deployer_client, "run_program",
                        lambda *a, **kw: sent.update(kw) or {"exit_code": 0,
                                                             "build_ok": True,
                                                             "deploy_ok": True})
    program = Program(id="11111111-1111-1111-1111-111111111111", title="P",
                      gitlab_repo_path="g/p", timeout_minutes=5,
                      cpu_limit="1", mem_limit="1g")
    instance = ProgramInstance(id="22222222-2222-2222-2222-222222222222",
                               program_id=program.id, org_id="o",
                               ssh_public_key="pk", ssh_private_key_enc="enc")
    run = ProgramRun(id="33333333-3333-3333-3333-333333333333",
                     program_id=program.id, instance_id=instance.id)

    programs_worker._execute(None, run, program, instance)
    assert sent["extra_host"] == "git.acme.test:10.1.2.3"


def test_execute_passes_an_empty_alias_when_unset(monkeypatch, tmp_path):
    sent = {}
    monkeypatch.setattr(settings, "git_extra_host", "")
    monkeypatch.setattr(settings, "workspaces_dir", str(tmp_path))
    monkeypatch.setattr(programs_worker, "_prepare", lambda *a, **k: True)
    monkeypatch.setattr(programs_worker, "_bill", lambda *a, **k: None)
    monkeypatch.setattr(programs_worker, "_finalize_outputs", lambda *a, **k: None)
    monkeypatch.setattr(programs_worker.deployer_client, "run_program",
                        lambda *a, **kw: sent.update(kw) or {"exit_code": 0,
                                                             "build_ok": True,
                                                             "deploy_ok": True})
    program = Program(id="11111111-1111-1111-1111-111111111111", title="P",
                      gitlab_repo_path="g/p", timeout_minutes=5,
                      cpu_limit="1", mem_limit="1g")
    run = ProgramRun(id="33333333-3333-3333-3333-333333333333",
                     program_id=program.id, kind="check")

    # a check run (no instance) takes the same path, so the admin dry run
    # reaches the forge exactly as a customer run does
    programs_worker._execute(None, run, program, None)
    assert sent["extra_host"] == ""
