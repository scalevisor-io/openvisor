"""Git knowledge sources (§KB, Part B): the admin API (SSH deploy-key generation,
HTTP PAT, the connection-check verify/enable gates and their secret hygiene) and the
multi-root reindex (namespacing that keeps two roots' same-named files from colliding,
skipping disabled/unverified sources, and one failing clone never aborting the rest).
"""
import base64
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import decrypt, encrypt
from app.core.security import hash_password
from app.main import app
from app.models import KnowledgeBase, Organization, User


@pytest.fixture(autouse=True)
def _cleanup_git_kbs():
    yield
    with SyncSession() as db:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.kind == "git"))
        db.commit()


# ------------------------------------------------------- repos.py secret hygiene

def test_https_with_pat_injects_and_encodes_token():
    from app.services import repos

    # token as PASSWORD under the oauth2 username - the only Basic form both
    # GitLab (401s token-as-username) and GitHub accept
    assert repos.https_with_pat("https://github.com/a/b.git", "ghp_x") == \
        "https://oauth2:ghp_x@github.com/a/b.git"
    # a token with URL-special chars is percent-encoded, not left raw
    out = repos.https_with_pat("https://gitlab.com/a/b.git", "a/b:c")
    assert "oauth2:a%2Fb%3Ac@gitlab.com" in out
    # an already-credentialed URL has its userinfo replaced, not appended
    assert repos.https_with_pat("https://old@github.com/a/b.git", "new") == \
        "https://oauth2:new@github.com/a/b.git"


def test_redact_secret_scrubs_token_and_userinfo():
    from app.services import repos

    msg = "fatal: unable to access 'https://glpat-SECRET@gitlab.com/a/b.git/': 403"
    red = repos.redact_secret(msg, "glpat-SECRET")
    assert "glpat-SECRET" not in red
    assert "***" in red


def test_check_http_git_never_leaks_pat(monkeypatch):
    """A failing ls-remote whose stderr echoes the credentialed URL must come back
    with the PAT scrubbed from the returned detail."""
    from app.services import repos

    pat = "glpat-TOPSECRET123"

    class _Proc:
        returncode = 128
        stdout = ""
        stderr = f"remote: HTTP Basic: Access denied\nfatal: Authentication failed for 'https://{pat}@gitlab.com/a/b.git/'"

    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: _Proc())
    ok, detail = repos.check_http_git("https://gitlab.com/a/b.git", pat)
    assert ok is False
    assert pat not in detail
    assert "authentication failed" in detail.lower()


def test_check_http_git_requires_https_and_token():
    from app.services import repos

    assert repos.check_http_git("git@github.com:a/b.git", "x")[0] is False  # not http
    assert repos.check_http_git("https://github.com/a/b.git", "")[0] is False  # no token


# ------------------------------------------------------ kb_git.py secret hygiene

def test_run_timeout_raises_generic_no_argv(monkeypatch):
    """A git command that times out must NOT surface subprocess.TimeoutExpired -
    its __str__ embeds the argv (which could carry a credentialed URL)."""
    from app.services import kb_git

    pat = "glpat-SECRET-IN-ARGV"

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["git", "clone", f"https://{pat}@h/r"], timeout=1)
    monkeypatch.setattr(kb_git.subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        kb_git._run(["git", "clone", "x"], {}, pat)
    assert pat not in str(ei.value)
    assert "timed out" in str(ei.value).lower()


def test_prepare_roots_redacts_secret_in_last_index_error(monkeypatch):
    """Defence in depth: even if a sync exception carries a credentialed URL, the
    persisted last_index_error (API-exposed) must be scrubbed."""
    from app.services import kb_git, rag

    monkeypatch.setattr(rag, "local_kb_enabled", lambda db: False)
    pat = "glpat-LEAKED-IN-EXC"
    kb = KnowledgeBase(kind="git", name="x", uri="https://gitlab.com/a/b.git",
                       auth_kind="http", api_key_enc=encrypt(pat), enabled=True,
                       verified=True, is_removable=True)
    with SyncSession() as db:
        db.add(kb)
        db.commit()
        kb_id = kb.id

    def _raise(_kb):
        raise RuntimeError(f"fatal: unable to access 'https://{pat}@gitlab.com/a/b.git/'")
    monkeypatch.setattr(kb_git, "sync_source", _raise)

    with SyncSession() as db:
        roots, errors = kb_git.prepare_roots(db)
    assert roots == [] and errors == 1
    with SyncSession() as db:
        err = db.get(KnowledgeBase, kb_id).last_index_error
    assert err and pat not in err


def test_sync_source_http_keeps_pat_out_of_url_and_argv(monkeypatch, tmp_path):
    """The PAT must never appear in the clone/fetch argv or the persisted origin: it
    rides as an ephemeral http.extraHeader passed via git config env, and every git
    command uses the CLEAN URL."""
    from app.services import kb_git

    monkeypatch.setattr(kb_git, "KB_GIT_ROOT", tmp_path)
    pat = "glpat-NEVER-IN-URL"
    uri = "https://gitlab.com/acme/docs.git"
    kb = KnowledgeBase(kind="git", id="kbhttp", name="d", uri=uri, auth_kind="http",
                       api_key_enc=encrypt(pat), ref="main", is_removable=True)

    calls = []

    def _rec(cmd, env, *secrets):
        calls.append((cmd, dict(env), secrets))
    monkeypatch.setattr(kb_git, "_run", _rec)

    # clone path (no .git yet)
    kb_git.sync_source(kb)
    clone_cmd, clone_env, secrets = calls[-1]
    assert clone_cmd[0:2] == ["git", "clone"]
    assert uri in clone_cmd and not any(pat in part for part in clone_cmd)  # clean URL
    hdr = clone_env["GIT_CONFIG_VALUE_0"]
    assert hdr.startswith("Authorization: Basic ") and pat not in hdr  # base64, not raw
    assert base64.b64decode(hdr.split()[-1]).decode() == f"oauth2:{pat}"
    assert pat in secrets  # redaction covers the raw PAT

    # fetch path (existing checkout): every command uses the clean URL; origin scrubbed
    (tmp_path / "kbhttp" / ".git").mkdir(parents=True)
    calls.clear()
    kb_git.sync_source(kb)
    for cmd, _env, _s in calls:
        assert not any(pat in part for part in cmd)
    seturl = [c for c in calls if c[0][3:5] == ["remote", "set-url"]][0][0]
    assert seturl[-1] == uri  # origin reset to the CLEAN url


# ------------------------------------------------------------------- HTTP surface

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _admin():
    email = f"kbgit-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "kb-secret-123"
    with SyncSession() as db:
        org = Organization(name="KB Git Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="admin", email_verified=True))
        db.commit()
    return email, pwd


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_git_kb_full_flow(client, monkeypatch):
    """One admin login exercises: SSH create (public key shown, private encrypted &
    never returned), HTTP create (PAT encrypted & never returned), verify sets the
    flag, and the enable gate re-runs the check server-side (409 on failure)."""
    from app.services import repos

    email, pwd = _admin()
    h = _auth(client, email, pwd)

    # -- SSH create: a deploy keypair is generated; the public key is returned, the
    #    private key is stored encrypted and never surfaced.
    r = client.post("/api/admin/knowledge-bases", headers=h, json={
        "kind": "git", "uri": "git@github.com:acme/handbook.git", "auth_kind": "ssh"})
    assert r.status_code == 200, r.text
    ssh = r.json()
    assert ssh["kind"] == "git" and ssh["auth_kind"] == "ssh"
    assert ssh["verified"] is False and ssh["enabled"] is False
    assert ssh["ssh_public_key"].startswith("ssh-ed25519 ")
    assert "ssh_private_key_enc" not in ssh and "private" not in str(ssh).lower()
    with SyncSession() as db:
        row = db.get(KnowledgeBase, ssh["id"])
        assert row.ssh_private_key_enc
        assert decrypt(row.ssh_private_key_enc).startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert row.ssh_public_key == ssh["ssh_public_key"]

    # -- SSH URL is validated
    assert client.post("/api/admin/knowledge-bases", headers=h, json={
        "kind": "git", "uri": "https://github.com/a/b.git", "auth_kind": "ssh"}).status_code == 400

    # -- HTTP create: the PAT is encrypted at rest, never returned
    pat = "glpat-abc123"
    r = client.post("/api/admin/knowledge-bases", headers=h, json={
        "kind": "git", "name": "Docs", "uri": "https://gitlab.com/acme/docs.git",
        "auth_kind": "http", "ref": "trunk", "api_key": pat})
    assert r.status_code == 200, r.text
    http = r.json()
    assert http["auth_kind"] == "http" and http["has_api_key"] is True
    assert http["ref"] == "trunk" and http["ssh_public_key"] is None
    assert "api_key" not in http and pat not in str(http)
    with SyncSession() as db:
        row = db.get(KnowledgeBase, http["id"])
        assert decrypt(row.api_key_enc) == pat

    # -- HTTP without a PAT is rejected
    assert client.post("/api/admin/knowledge-bases", headers=h, json={
        "kind": "git", "uri": "https://gitlab.com/a/b.git", "auth_kind": "http"}).status_code == 422

    # -- no secret leaks through the list endpoint
    listing = client.get("/api/admin/knowledge-bases", headers=h).json()
    assert pat not in str(listing)
    assert all("ssh_private_key_enc" not in r and "api_key_enc" not in r for r in listing)

    # -- verify (SSH): mock the reachability check ok -> verified true
    monkeypatch.setattr(repos, "check_ssh", lambda uri, key: (True, "reachable"))
    v = client.post(f"/api/admin/knowledge-bases/{ssh['id']}/verify", headers=h)
    assert v.status_code == 200 and v.json()["ok"] is True
    with SyncSession() as db:
        assert db.get(KnowledgeBase, ssh["id"]).verified is True

    # -- verify failing -> verified false
    monkeypatch.setattr(repos, "check_ssh", lambda uri, key: (False, "permission denied"))
    v = client.post(f"/api/admin/knowledge-bases/{ssh['id']}/verify", headers=h)
    assert v.status_code == 200 and v.json()["ok"] is False
    with SyncSession() as db:
        assert db.get(KnowledgeBase, ssh["id"]).verified is False

    # -- enabling re-runs the check server-side and 409s when it fails (never trust
    #    the client: a still-failing source can't be flipped on).
    r = client.patch(f"/api/admin/knowledge-bases/{ssh['id']}", headers=h, json={"enabled": True})
    assert r.status_code == 409
    with SyncSession() as db:
        assert db.get(KnowledgeBase, ssh["id"]).enabled is False

    # -- with the check passing, enable succeeds and flips verified+enabled on
    monkeypatch.setattr(repos, "check_ssh", lambda uri, key: (True, "ok"))
    r = client.patch(f"/api/admin/knowledge-bases/{ssh['id']}", headers=h, json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True and r.json()["verified"] is True

    # -- editing the branch re-arms verification (must be re-checked before enable)
    r = client.patch(f"/api/admin/knowledge-bases/{ssh['id']}", headers=h, json={"ref": "release"})
    assert r.status_code == 200 and r.json()["verified"] is False and r.json()["ref"] == "release"

    # -- git rows are removable
    assert client.delete(f"/api/admin/knowledge-bases/{http['id']}", headers=h).status_code == 200


# ------------------------------------------------------------ multi-root ingest

def test_multi_root_reindex_namespaces_and_unions(monkeypatch, tmp_path):
    """Two roots, each with a same-named README.md, index into ONE reindex with
    namespaced non-colliding ids/paths - neither overwrites the other."""
    from app.services import kb_classify, meili, rag

    monkeypatch.setattr(kb_classify, "_llm_classify", lambda blocks: None)  # -> fact

    local = tmp_path / "local"
    gitkb = tmp_path / "kbid"
    local.mkdir()
    gitkb.mkdir()
    (local / "README.md").write_text("Local knowledge about sovereign hosting.")
    (gitkb / "README.md").write_text("Git repo knowledge, entirely different text.")
    (gitkb / "guide.md").write_text("A second git file.")

    monkeypatch.setattr(rag, "embed", lambda texts: [[0.1] * rag.EMBED_DIM for _ in texts])
    captured = {"calls": 0}

    def _reindex(docs):
        captured["calls"] += 1
        captured["docs"] = docs
        return len(docs)
    monkeypatch.setattr(meili, "reindex_kb", _reindex)

    n = rag.ingest_knowledge_repo([("local", local), ("kbid", gitkb)])
    assert captured["calls"] == 1  # reindexed ONCE with the union
    docs = captured["docs"]
    assert n == len(docs) == 3
    ids = [d["id"] for d in docs]
    assert len(set(ids)) == 3  # no id collision across roots
    paths = {d["path"] for d in docs}
    assert "local/README.md#0" in paths and "kbid/README.md#0" in paths
    files = {d["file"] for d in docs}
    assert "local/README.md" in files and "kbid/README.md" in files


def test_tree_fingerprint_is_root_order_insensitive(tmp_path):
    """Regression (prod regression): prepare_roots' SELECT had no ORDER BY, and the
    fingerprint joined per-root parts in list order - every run rewrites the KB rows
    (last_indexed_at), Postgres reshuffles them, and with several git sources the
    permuting string never matched the stored one, so the beat fully re-embedded the
    whole corpus every 5 minutes forever. The same roots must fingerprint identically
    whatever their enumeration order; a changed tree must still change it."""
    from app.services import rag

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.md").write_text("alpha knowledge")
    (b / "two.md").write_text("beta knowledge")

    fp = rag.kb_tree_fingerprint([("local", a), ("kbid", b)])
    assert rag.kb_tree_fingerprint([("kbid", b), ("local", a)]) == fp
    (b / "two.md").write_text("beta knowledge grew longer")
    assert rag.kb_tree_fingerprint([("local", a), ("kbid", b)]) != fp


def test_git_only_kb_still_retrieves_when_local_disabled(monkeypatch):
    """A git-only KB (local disabled but a git source enabled+verified) must still
    query the shared index - retrieval short-circuits only when NO source is active."""
    from app.services import meili, rag
    from app.seed import seed_knowledge_bases

    seed_knowledge_bases()
    calls = []
    monkeypatch.setattr(rag, "_embed_raw", lambda texts: calls.append(texts) or ([[0.0]], {}))
    monkeypatch.setattr(meili, "search_hybrid", lambda vec, q, k, tags=None: [])

    with SyncSession() as db:
        db.execute(select(KnowledgeBase).where(
            KnowledgeBase.kind == "local")).scalar_one().enabled = False
        db.add(KnowledgeBase(kind="git", name="g", uri="git@h:a/b.git", auth_kind="ssh",
                             enabled=True, verified=True, is_removable=True))
        db.commit()

    with SyncSession() as db:
        # local off + git live -> retrieval proceeds (query embedded)
        assert rag.kb_retrieval_enabled(db) is True
        rag.retrieve(db, "anything")
    assert calls, "git-only KB should still embed + query"

    with SyncSession() as db:  # restore the local row for other tests
        db.execute(select(KnowledgeBase).where(
            KnowledgeBase.kind == "local")).scalar_one().enabled = True
        db.commit()


def test_prepare_roots_skips_disabled_and_isolates_failures(monkeypatch, tmp_path):
    """prepare_roots clones only enabled+verified git sources; a disabled/unverified
    one is never touched, and a source whose sync raises records last_index_error
    without aborting the others."""
    from app.services import kb_git, rag

    monkeypatch.setattr(rag, "local_kb_enabled", lambda db: False)  # isolate git roots

    good = KnowledgeBase(kind="git", name="good", uri="git@h:a/b.git", auth_kind="ssh",
                         enabled=True, verified=True, is_removable=True)
    bad = KnowledgeBase(kind="git", name="bad", uri="git@h:c/d.git", auth_kind="ssh",
                        enabled=True, verified=True, is_removable=True)
    off = KnowledgeBase(kind="git", name="off", uri="git@h:e/f.git", auth_kind="ssh",
                        enabled=False, verified=True, is_removable=True)
    unver = KnowledgeBase(kind="git", name="unver", uri="git@h:g/h.git", auth_kind="ssh",
                          enabled=True, verified=False, is_removable=True)
    with SyncSession() as db:
        for kb in (good, bad, off, unver):
            db.add(kb)
        db.commit()
        good_id, bad_id = good.id, bad.id

    goodpath = tmp_path / "good"
    goodpath.mkdir()
    attempted = []

    def _sync(kb):
        attempted.append(kb.id)
        if kb.id == bad_id:
            raise RuntimeError("clone failed: permission denied")
        return goodpath
    monkeypatch.setattr(kb_git, "sync_source", _sync)

    with SyncSession() as db:
        roots, errors = kb_git.prepare_roots(db)

    # only the two enabled+verified sources were attempted (disabled/unverified skipped)
    assert set(attempted) == {good_id, bad_id}
    assert roots == [(good_id, goodpath)]  # local excluded, bad excluded
    assert errors == 1  # the one failing source is counted
    with SyncSession() as db:
        assert db.get(KnowledgeBase, bad_id).last_index_error
        assert db.get(KnowledgeBase, good_id).last_index_error is None
        assert db.get(KnowledgeBase, good_id).last_indexed_at is not None


def test_kb_files_skips_symlinks_outside_root(tmp_path):
    """An untrusted git source can commit a symlink pointing outside its checkout;
    its target must never be indexed - a leaf symlink is skipped, and a file reached
    through a symlinked parent dir fails the realpath-containment check."""
    from app.services import rag

    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.md").write_text("inside content")
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET OUTSIDE THE ROOT")
    (root / "evil.md").symlink_to(secret)  # leaf symlink to an outside file

    outside_dir = tmp_path / "outdir"
    outside_dir.mkdir()
    (outside_dir / "leak.md").write_text("LEAK VIA SYMLINKED DIR")
    (root / "sub").symlink_to(outside_dir, target_is_directory=True)

    names = {p.name for p in rag._kb_files(root)}
    assert "ok.md" in names
    assert "evil.md" not in names
    assert "leak.md" not in names


def test_wipe_guard_keeps_index_when_all_sources_error(monkeypatch):
    """reindex_kb full-replaces the index; a transient failure of the only active
    source (0 docs + errors) must keep the existing index, not swap it to empty. A
    genuinely empty KB (0 docs, no errors) still reindexes."""
    from app.services import meili, rag

    calls = {"n": 0}
    monkeypatch.setattr(meili, "reindex_kb",
                        lambda docs: calls.__setitem__("n", calls["n"] + 1) or len(docs))

    assert rag.ingest_knowledge_repo([], had_source_errors=True) == -1
    assert calls["n"] == 0  # index NOT wiped
    assert rag.ingest_knowledge_repo([], had_source_errors=False) == 0
    assert calls["n"] == 1  # legitimately empty -> reindex proceeds


def test_sync_source_real_git_origin_has_no_credential(tmp_path, monkeypatch):
    """End-to-end against a real local git remote: clone then fetch, and assert the
    persisted origin (and .git/config) never carries the credential."""
    from app.services import kb_git

    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello from the remote")
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "origin", "main"], check=True, capture_output=True, env=env)

    monkeypatch.setattr(kb_git, "KB_GIT_ROOT", tmp_path / "clones")
    pat = "glpat-DUMMY-TOKEN"
    uri = f"file://{bare}"
    kb = KnowledgeBase(kind="git", id="real", name="r", uri=uri, auth_kind="http",
                       api_key_enc=encrypt(pat), ref="main", is_removable=True)

    dest = kb_git.sync_source(kb)  # clone
    assert (dest / "README.md").read_text() == "hello from the remote"
    cfg = (dest / ".git" / "config").read_text()
    assert pat not in cfg and "extraHeader" not in cfg  # header never persisted
    dest = kb_git.sync_source(kb)  # fetch path
    cfg = (dest / ".git" / "config").read_text()
    assert pat not in cfg
