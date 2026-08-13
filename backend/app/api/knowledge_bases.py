"""Admin management of the instance's knowledge bases (§KB).

Instance-admin-level (the spoke owner's, not per customer org): one global list.
Five kinds - `local` (the /knowledge Meilisearch KB), `context7` (the repo's
Context7 MCP), `mcp` (a generic admin-added MCP endpoint), `websearch` (seeded
web-search providers - uri is the provider slug; key + enable are the only
editable fields, and enabling re-verifies the key against the provider) and
`git` (a git repo cloned by the worker and indexed alongside /knowledge). The
`local`, `context7` and `websearch` rows are seeded and non-removable; `mcp` and
`git` rows are user-added, fully editable and removable. API keys / PATs are
envelope-encrypted at rest and NEVER returned (only `has_api_key: bool`). A git
SSH source exposes its PUBLIC deploy key (safe to show) but never the private half.
"""
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import require_admin
from app.core.encryption import decrypt, encrypt
from app.models import KnowledgeBase
from app.schemas.schemas import (KbBlockOverrideIn, KnowledgeBaseCreateIn,
                                 KnowledgeBasePatchIn)
from app.services import mcp_names, repos, sshkeys, websearch

router = APIRouter(prefix="/api/admin/knowledge-bases", tags=["knowledge-bases"],
                   dependencies=[Depends(require_admin)])

_HTTP_RE = re.compile(r"^https?://", re.I)


def _kb_out(kb: KnowledgeBase) -> dict:
    """Serialize a KB for the admin UI. The api_key/PAT and the SSH private key are
    never exposed - only whether a key is set - so no secret can be read back out of
    the API. A git SSH source exposes its PUBLIC deploy key so the UI can show it."""
    out = {
        "id": kb.id,
        "kind": kb.kind,
        "name": kb.name,
        "enabled": kb.enabled,
        "uri": kb.uri,
        "has_api_key": bool(kb.api_key_enc),
        "is_removable": kb.is_removable,
        "sort_order": kb.sort_order,
        "created_at": kb.created_at,
        # What a dev run calls this source. null for retrieval-only kinds
        # (local/git), whose content arrives in the task instead of as a tool -
        # so instructions can name the tool sources and nothing else.
        "mcp_server": mcp_names.kb_server_name(kb),
        "mcp_tools": mcp_names.kb_tools(kb),
    }
    if kb.kind == "git":
        out.update({
            "auth_kind": kb.auth_kind,
            "http_username": kb.http_username if kb.auth_kind == "http" else None,
            "ref": kb.ref,
            "ssh_public_key": kb.ssh_public_key if kb.auth_kind == "ssh" else None,
            "verified": kb.verified,
            "last_indexed_at": kb.last_indexed_at,
            "last_index_error": kb.last_index_error,
        })
    return out


def _validate_uri(uri: str) -> str:
    u = uri.strip()
    if not _HTTP_RE.match(u):
        raise HTTPException(400, "URL must start with http:// or https://")
    return u


def _git_default_name(uri: str) -> str:
    """A friendly fallback name for a git source: host + last path segment."""
    u = (uri or "").strip()
    if repos.is_ssh_uri(u) and "://" not in u:  # scp-like git@host:path
        host = u.split("@", 1)[-1].split(":", 1)[0]
        path = u.split(":", 1)[-1]
    else:
        parts = urlsplit(u)
        host = parts.netloc.rsplit("@", 1)[-1]
        path = parts.path
    tail = path.strip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return f"{host}/{tail}".strip("/") or (host or "git source")


async def _run_git_check(kb: KnowledgeBase) -> tuple[bool, str]:
    """Run the connection check for a git row in a threadpool (both branches shell
    out to git ls-remote). SSH decrypts the deploy key; HTTP decrypts the PAT."""
    if kb.auth_kind == "ssh":
        key = decrypt(kb.ssh_private_key_enc) if kb.ssh_private_key_enc else ""
        return await run_in_threadpool(repos.check_ssh, kb.uri, key)
    pat = decrypt(kb.api_key_enc) if kb.api_key_enc else ""
    return await run_in_threadpool(repos.check_http_git, kb.uri, pat, kb.http_username)


@router.get("")
async def list_knowledge_bases(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(KnowledgeBase).order_by(
        KnowledgeBase.sort_order, KnowledgeBase.created_at))).scalars().all()
    return [_kb_out(kb) for kb in rows]


@router.post("")
async def create_knowledge_base(body: KnowledgeBaseCreateIn,
                                db: AsyncSession = Depends(get_db)):
    """Add a knowledge base. `git` sources are created disabled+unverified (they must
    pass the connection check and be explicitly enabled first). `mcp` is the default
    kind; the built-in local/context7 KBs are seeded, not created here."""
    kind = (body.kind or "mcp").strip()
    if kind == "git":
        return await _create_git_kb(body, db)
    if kind != "mcp":
        raise HTTPException(400, "Unsupported knowledge base kind")
    if not (body.name or "").strip():
        raise HTTPException(422, "A name is required")
    uri = _validate_uri(body.uri)
    kb = KnowledgeBase(kind="mcp", name=body.name.strip(), enabled=True, uri=uri,
                       api_key_enc=encrypt(body.api_key) if body.api_key else None,
                       is_removable=True)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return _kb_out(kb)


async def _create_git_kb(body: KnowledgeBaseCreateIn,
                         db: AsyncSession) -> dict:
    auth_kind = (body.auth_kind or "").strip().lower()
    if auth_kind not in ("ssh", "http"):
        raise HTTPException(422, "auth_kind must be 'ssh' or 'http' for a git source")
    uri = (body.uri or "").strip()
    ref = (body.ref or "main").strip() or "main"
    name = (body.name or "").strip() or _git_default_name(uri)
    kb = KnowledgeBase(kind="git", name=name, uri=uri, ref=ref, auth_kind=auth_kind,
                       enabled=False, verified=False, is_removable=True)
    if auth_kind == "ssh":
        if not repos.is_ssh_uri(uri):
            raise HTTPException(400, "For SSH, use a git@host:path or ssh:// URL")
        priv, pub = sshkeys.generate_keypair(comment=f"kb-{_git_default_name(uri)}")
        kb.ssh_public_key = pub
        kb.ssh_private_key_enc = encrypt(priv)
    else:  # http
        if not _HTTP_RE.match(uri):
            raise HTTPException(400, "For HTTP(S), the URL must start with http:// or https://")
        if not (body.api_key or "").strip():
            raise HTTPException(422, "An access token is required for an HTTP(S) git source")
        kb.api_key_enc = encrypt(body.api_key.strip())
        kb.http_username = (body.http_username or "").strip() or None
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return _kb_out(kb)


@router.post("/{kb_id}/verify")
async def verify_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    """Run the connection check for a git source (git ls-remote over SSH/HTTP) and
    persist the result on `verified`. Only git rows are verifiable. Returns
    {ok, detail}; the detail never leaks the PAT or the deploy key."""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "Knowledge base not found")
    if kb.kind != "git":
        raise HTTPException(400, "Only git knowledge bases can be verified")
    ok, detail = await _run_git_check(kb)
    kb.verified = ok
    await db.commit()
    return {"ok": ok, "detail": detail}


@router.patch("/{kb_id}")
async def update_knowledge_base(kb_id: str, body: KnowledgeBasePatchIn,
                                db: AsyncSession = Depends(get_db)):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "Knowledge base not found")
    fields = body.model_dump(exclude_unset=True)
    if kb.kind in ("local", "context7"):
        # Built-in KBs: only their enable flag can change.
        if set(fields) - {"enabled"}:
            raise HTTPException(400, "Only the enabled flag is editable for a built-in knowledge base")
        if "enabled" in fields:
            kb.enabled = fields["enabled"]
        await db.commit()
        await db.refresh(kb)
        return _kb_out(kb)
    if kb.kind == "websearch":
        return await _update_websearch_kb(kb, fields, db)
    if kb.kind == "git":
        return await _update_git_kb(kb, fields, db)
    # mcp rows accept every field.
    if "enabled" in fields:
        kb.enabled = fields["enabled"]
    if "name" in fields:
        kb.name = fields["name"].strip()
    if "uri" in fields:
        kb.uri = _validate_uri(fields["uri"])
    if "api_key" in fields:
        # Provided → re-encrypt; empty string → clear the stored key.
        kb.api_key_enc = encrypt(fields["api_key"]) if fields["api_key"] else None
    await db.commit()
    await db.refresh(kb)
    return _kb_out(kb)


async def _update_websearch_kb(kb: KnowledgeBase, fields: dict,
                               db: AsyncSession) -> dict:
    """Edit a seeded websearch source: only the API key and the enable flag move
    (name/uri are fixed - uri IS the provider slug). Enabling ALWAYS re-verifies
    the stored key against the provider server-side and 409s unless it passes -
    the same never-trust-the-client discipline as git sources."""
    if set(fields) - {"enabled", "api_key"}:
        raise HTTPException(400, "Only the API key and the enabled flag are editable for a web-search source")
    if "api_key" in fields:
        kb.api_key_enc = encrypt(fields["api_key"]) if fields["api_key"] else None
        if not kb.api_key_enc:
            kb.enabled = False  # a keyless source can't stay live
    if fields.get("enabled"):
        key = decrypt(kb.api_key_enc) if kb.api_key_enc else ""
        ok, detail = await run_in_threadpool(websearch.verify_key, kb.uri, key)
        if not ok:
            raise HTTPException(409, f"Can't enable this web-search source: {detail}")
        kb.enabled = True
    elif fields.get("enabled") is False:
        kb.enabled = False
    await db.commit()
    await db.refresh(kb)
    return _kb_out(kb)


async def _update_git_kb(kb: KnowledgeBase, fields: dict,
                         db: AsyncSession) -> dict:
    """Edit a git source. Changing the repo URL / branch / PAT invalidates the prior
    verification (must be re-checked). Enabling ALWAYS re-runs the connection check
    server-side against the current config and is rejected (409) unless it passes -
    never trust the client. A disabled→enabled transition on a failing source fails."""
    changed_target = False
    if "name" in fields and fields["name"] is not None:
        kb.name = fields["name"].strip()
    if "ref" in fields and fields["ref"] is not None:
        kb.ref = (fields["ref"].strip() or "main")
        changed_target = True
    if "uri" in fields and fields["uri"] is not None:
        new_uri = fields["uri"].strip()
        if kb.auth_kind == "ssh" and not repos.is_ssh_uri(new_uri):
            raise HTTPException(400, "For SSH, use a git@host:path or ssh:// URL")
        if kb.auth_kind == "http" and not _HTTP_RE.match(new_uri):
            raise HTTPException(400, "For HTTP(S), the URL must start with http:// or https://")
        kb.uri = new_uri
        changed_target = True
    if "api_key" in fields and fields["api_key"]:
        # Only meaningful for an HTTP source (re-set the PAT); ignored for SSH.
        if kb.auth_kind == "http":
            kb.api_key_enc = encrypt(fields["api_key"])
            changed_target = True
    if "http_username" in fields and fields["http_username"] is not None:
        # HTTP only: the Basic username paired with the token (empty → oauth2).
        if kb.auth_kind == "http":
            kb.http_username = fields["http_username"].strip() or None
            changed_target = True
    if changed_target:
        kb.verified = False
    if "enabled" in fields and fields["enabled"] is not None:
        if fields["enabled"]:
            ok, detail = await _run_git_check(kb)
            kb.verified = ok
            if not ok:
                # Nothing is committed - the row keeps its prior state.
                raise HTTPException(409, f"Can't enable this source: {detail}")
            kb.enabled = True
        else:
            kb.enabled = False
    await db.commit()
    await db.refresh(kb)
    return _kb_out(kb)


@router.delete("/{kb_id}")
async def delete_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "Knowledge base not found")
    if not kb.is_removable:
        raise HTTPException(400, "This knowledge base can't be removed")
    await db.delete(kb)
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------- §KB tiers admin

def _collect_blocks(root_key: str) -> list[dict]:
    """Group the root's Meili chunks back into blocks (first chunk = excerpt), in
    file order then chunk order. Runs in a threadpool - the meili client is sync."""
    from app.services import meili

    by_hash: dict[str, dict] = {}
    prefix = f"{root_key}/"
    for doc in meili.iter_kb_docs("content,file,path,content_class,block_hash"):
        file = doc.get("file") or ""
        if not file.startswith(prefix):
            continue
        bh = doc.get("block_hash")
        if not bh:
            continue
        try:
            idx = int((doc.get("path") or "#0").rsplit("#", 1)[-1])
        except ValueError:
            idx = 0
        cur = by_hash.get(bh)
        if cur is None:
            by_hash[bh] = {"block_hash": bh, "rel": file[len(prefix):],
                           "content_class": doc.get("content_class") or "fact",
                           "excerpt": doc.get("content", "")[:300],
                           "chunks": 1, "_idx": idx}
        else:
            cur["chunks"] += 1
            if idx < cur["_idx"]:
                cur["_idx"] = idx
                cur["excerpt"] = doc.get("content", "")[:300]
    blocks = sorted(by_hash.values(), key=lambda b: (b["rel"], b["_idx"]))
    for b in blocks:
        b.pop("_idx")
    return blocks


@router.get("/{kb_id}/tiers")
async def kb_tiers(kb_id: str, content_class: str | None = None, page: int = 1,
                   per_page: int = 50, db: AsyncSession = Depends(get_db)):
    """§KB tiers derived-split view for a RETRIEVAL source (local/git): the compiled
    standing-rules digest, per-class block counts over the WHOLE source, and one
    PAGE of blocks with class, origin (override|llm|auto) and first-chunk excerpt -
    the admin correction surface for classifier errors. `content_class` narrows the
    listing to one tier and `page`/`per_page` (clamped to 200) window it, so a
    large corpus stays browsable; `total` is the filtered block count while
    `counts` always describes the full source (stable chips)."""
    from app.models import KbBlockClass, KbRulesDigest

    if content_class is not None and content_class not in ("fact", "rule", "procedure"):
        raise HTTPException(422, "content_class must be fact, rule or procedure")
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(404, "Knowledge base not found")
    if kb.kind not in ("local", "git"):
        raise HTTPException(400, "Content tiers exist for retrieval sources only")
    root_key = "local" if kb.kind == "local" else kb.id
    blocks = await run_in_threadpool(_collect_blocks, root_key)
    counts = {"fact": 0, "rule": 0, "procedure": 0}
    for b in blocks:
        counts[b["content_class"]] = counts.get(b["content_class"], 0) + 1
    if content_class is not None:
        blocks = [b for b in blocks if b["content_class"] == content_class]
    total = len(blocks)
    blocks = blocks[(page - 1) * per_page:page * per_page]
    hashes = [b["block_hash"] for b in blocks]
    origins: dict[str, str] = {}
    if hashes:
        rows = (await db.execute(select(KbBlockClass).where(
            KbBlockClass.content_hash.in_(hashes)))).scalars().all()
        origins = {r.content_hash: r.origin for r in rows}
    for b in blocks:
        # 'auto' = classified by a path/frontmatter signal or the per-run fallback
        # (neither is cached); 'llm' = cached model verdict; 'override' = admin pin.
        b["origin"] = origins.get(b["block_hash"], "auto")
    digest_row = (await db.execute(select(KbRulesDigest).where(
        KbRulesDigest.root_key == root_key))).scalar_one_or_none()
    digest = None
    if digest_row is not None:
        digest = {"content": digest_row.content, "char_count": digest_row.char_count,
                  "compiled_at": digest_row.compiled_at}
    return {"digest": digest, "counts": counts, "blocks": blocks,
            "total": total, "page": page, "per_page": per_page}


@router.put("/blocks/{content_hash}")
async def override_block_class(content_hash: str, body: KbBlockOverrideIn,
                               db: AsyncSession = Depends(get_db)):
    """Pin a block's class (§KB tiers): upsert the kb_block_class row as
    origin='override' - it beats signals and the classifier, and survives reindexes
    until the block's text (and so its hash) changes - then dispatch a forced
    reindex so the index, digests and procedure registry reflect it. Async like
    /admin/knowledge/reindex."""
    from app.models import KbBlockClass
    from app.workers.celery_app import celery

    row = await db.get(KbBlockClass, content_hash)
    if row is None:
        row = KbBlockClass(content_hash=content_hash,
                           content_class=body.content_class, origin="override")
        db.add(row)
    else:
        row.content_class = body.content_class
        row.origin = "override"
    await db.commit()
    celery.send_task("app.workers.tasks.ingest_knowledge", args=[True])
    return {"content_hash": content_hash, "content_class": body.content_class,
            "origin": "override", "reindex": "dispatched"}


@router.delete("/blocks/{content_hash}")
async def clear_block_override(content_hash: str, db: AsyncSession = Depends(get_db)):
    """Revert a block to automatic classification: only override rows can be
    cleared (an llm cache row is not admin state), and the forced reindex
    re-classifies the block from scratch."""
    from app.models import KbBlockClass
    from app.workers.celery_app import celery

    row = await db.get(KbBlockClass, content_hash)
    if row is None or row.origin != "override":
        raise HTTPException(404, "No override for this block")
    await db.delete(row)
    await db.commit()
    celery.send_task("app.workers.tasks.ingest_knowledge", args=[True])
    return {"ok": True, "reindex": "dispatched"}
