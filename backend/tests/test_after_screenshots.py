"""§After-shots: boot-gate screenshots posted as one comment on the published
change. Provider-neutral contract: _remote_ops providers that implement both
`upload` (bytes -> image markdown) and `comment` get shots for free; a provider
without API image hosting (GitHub) omits `upload` and is skipped; a future
provider opts in by adding the two lambdas. Best-effort - never fails a publish."""
import base64
from types import SimpleNamespace

from app.services import github, gitlab
from app.workers import tasks

PNG_B64 = base64.b64encode(b"\x89PNG-fake").decode()


def _shots():
    return [{"width": 1280, "height": 800, "png_b64": PNG_B64},
            {"width": 390, "height": 844, "png_b64": PNG_B64}]


def _project():
    p = SimpleNamespace(id="p1")
    p._boot_screenshots = _shots()
    return p


def test_gitlab_ops_carry_the_screenshot_capabilities(monkeypatch):
    monkeypatch.setattr(gitlab, "customer_upload_file", lambda *a: "![x](/uploads/x)")
    monkeypatch.setattr(gitlab, "customer_create_mr_note", lambda *a: None)
    ops = tasks._remote_ops({"provider": "gitlab", "base_url": "https://gl", "path": "g/p",
                             "base_branch": "main"}, "tok")
    assert "upload" in ops and "comment" in ops


def test_github_ops_have_comment_but_no_upload():
    ops = tasks._remote_ops({"provider": "github", "owner": "o", "repo": "r",
                             "base_branch": "main"}, "tok")
    assert "comment" in ops
    assert "upload" not in ops  # no API image hosting - After-shots must skip


def test_publish_uploads_each_shot_and_posts_one_comment(monkeypatch):
    uploads, comments = [], []
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    ops = {"upload": lambda fn, data: (uploads.append((fn, data)), f"![{fn}](/uploads/{fn})")[1],
           "comment": lambda number, body: comments.append((number, body))}
    project = _project()
    tasks._publish_after_screenshots(ops, 12, project)
    assert [u[0] for u in uploads] == ["after-12-1280x800.png", "after-12-390x844.png"]
    assert all(u[1] == b"\x89PNG-fake" for u in uploads)
    (number, body), = comments
    assert number == 12 and body.startswith("## After")
    assert "Desktop (1280×800)" in body and "Mobile (390×844)" in body
    assert body.count("/uploads/") == 2
    assert project._boot_screenshots == []  # consumed - never reposted stale


def test_publish_skips_without_upload_capability():
    calls = []
    ops = {"comment": lambda number, body: calls.append(body)}
    project = _project()
    tasks._publish_after_screenshots(ops, 12, project)
    assert calls == []
    assert project._boot_screenshots == []  # still consumed


def test_publish_is_best_effort(monkeypatch):
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    def boom(fn, data):
        raise RuntimeError("uploads api down")
    # must not raise - a failed screenshot post never fails the publish
    tasks._publish_after_screenshots({"upload": boom, "comment": lambda *a: None},
                                     12, _project())


def test_publish_noop_without_stash():
    called = []
    ops = {"upload": lambda *a: called.append("u"), "comment": lambda *a: called.append("c")}
    tasks._publish_after_screenshots(ops, 12, SimpleNamespace(id="p1"))
    assert called == []
