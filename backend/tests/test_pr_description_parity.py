"""§PR description parity, customer path: open() returns a pre-existing open
PR/MR untouched, so a revise run's fresh .openvisor/pr.md must be pushed onto
the change explicitly (prod: an MR kept claiming "no browser available;
verified by static grep" while its newest commits carried real viewport
verification). The refresh only fires when THIS run authored a summary - a
summary-less run keeps the existing description (never a downgrade) - and is
best-effort like the platform-path twin."""
from app.services import github, gitlab
from app.workers import tasks


def _gh_target():
    return {"provider": "github", "owner": "o", "repo": "r", "base_branch": "main"}


def _gl_target():
    return {"provider": "gitlab", "base_url": "https://gl.example.com", "path": "g/p",
            "base_branch": "main"}


def test_describe_op_wires_github(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "update_pr_body",
                        lambda owner, repo, number, body, token=None:
                        calls.append((owner, repo, number, body, token)))
    ops = tasks._remote_ops(_gh_target(), "tok", "agent/x")
    ops["describe"](7, "new body")
    assert calls == [("o", "r", 7, "new body", "tok")]


def test_describe_op_wires_customer_gitlab(monkeypatch):
    calls = []
    monkeypatch.setattr(gitlab, "customer_update_mr_desc",
                        lambda base_url, token, path, iid, description:
                        calls.append((base_url, token, path, iid, description)))
    ops = tasks._remote_ops(_gl_target(), "tok", "agent/x")
    ops["describe"](12, "new body")
    assert calls == [("https://gl.example.com", "tok", "g/p", 12, "new body")]


def test_refresh_fires_only_with_an_agent_summary():
    calls = []
    ops = {"describe": lambda number, body: calls.append((number, body))}
    tasks._refresh_change_description(ops, 12, "body", None, "pid")
    assert calls == []  # no summary from this run: keep the existing description
    tasks._refresh_change_description(ops, 12, "body", "summary", "pid")
    assert calls == [(12, "body")]


def test_refresh_is_best_effort():
    def boom(number, body):
        raise RuntimeError("api down")
    # must not raise - a failed description refresh never fails the publish
    tasks._refresh_change_description({"describe": boom}, 12, "body", "summary", "pid")
