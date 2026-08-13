"""KB content-tier classification (§KB tiers).

Humans write blended knowledge documents - company facts, standing rules and
step-by-step procedures in one README - and keep doing so. Rather than forcing
authors to restructure their sources, ingestion classifies every markdown BLOCK and
derives the three consumption tiers from the one corpus: `fact` chunks feed hybrid
retrieval unchanged, `rule` blocks compile into a per-source standing-rules digest
injected into every dev run whose project selects the source, `procedure` blocks are
task-shaped workflows (registry consumed at dispatch - §KB tiers phase 2). Everything
is STILL indexed for retrieval whatever its class: the tiers are additive.

Deterministic signals beat the model: a frontmatter `type:` (Rule/Principle/
Procedure...), an AGENTS.md/CLAUDE.md file, or a skills/ path classifies the whole
file with no LLM call. Unsignalled markdown goes to the classifier LLM one call per
file, UNBILLED (platform-side cost, like ingest embedding); any LLM failure degrades
that file to `fact` for this run only AND flags the run degraded, so ingestion never
breaks, the fallback is never cached, and the ingest keeps the previous standing-rules
digests and procedures instead of replacing them from fallback verdicts.
LLM verdicts are cached in kb_block_class by content hash; admin overrides
(origin='override', written by the admin KB page) share the table and always win.
Signal verdicts are NOT cached: they derive from the file path, and one block's text
can legitimately appear under two paths with two different signals.
"""
import hashlib
import logging
import re
from pathlib import Path

import yaml
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SyncSession
from app.models import KbBlockClass
from app.services import brand
from app.services.llm import chat_json

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parents[1] / "agents" / "prompts"

CLASSES = ("fact", "rule", "procedure")

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx"}

# Frontmatter `type:` values -> class. Principles/conventions instruct (they shape
# HOW the agent works), so they join the rule tier alongside explicit rules.
FRONTMATTER_CLASSES = {
    "rule": "rule", "rules": "rule", "policy": "rule", "principle": "rule",
    "convention": "rule", "guideline": "rule",
    "procedure": "procedure", "howto": "procedure", "how-to": "procedure",
    "skill": "procedure", "runbook": "procedure", "workflow": "procedure",
}

# File names that ARE standing instructions by ecosystem convention.
RULE_FILE_NAMES = {"agents.md", "claude.md"}
# A SKILL.md, or any markdown under a skills/ directory, is a procedure.
PROCEDURE_FILE_NAMES = {"skill.md"}
PROCEDURE_DIR = "skills"

# Section splitting: a markdown heading of any level starts a new block; a section
# larger than this re-splits on paragraph boundaries so one heading-less README
# doesn't become a single monolithic block (the whole point is that blended files
# get classified block by block).
MAX_BLOCK_CHARS = 2000

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)

_LLM_BLOCK_CHARS = 1200  # per-block excerpt sent to the classifier
_LLM_MAX_BLOCKS = 40     # hard cap per call; the rest of the file defaults to fact


def norm_hash(text: str) -> str:
    """Content key for kb_block_class: blake2b over whitespace-normalized text, so
    reformatting that doesn't change words keeps the cached verdict and any admin
    override."""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.blake2b(norm.encode(), digest_size=16).hexdigest()


def _load_prompt(name: str) -> str:
    return brand.render((PROMPT_DIR / name).read_text())


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """(meta, body). A leading `--- yaml ---` block is removed from the body so it
    is neither classified nor indexed as prose; malformed YAML -> ({}, text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    return meta, text[m.end():]


def file_signal(rel: str, meta: dict) -> str | None:
    """Deterministic file-level class, or None when the LLM should decide.
    Precedence: frontmatter `type:` > file-name/path convention."""
    fm_type = str(meta.get("type", "")).strip().lower()
    if fm_type in FRONTMATTER_CLASSES:
        return FRONTMATTER_CLASSES[fm_type]
    parts = [p.lower() for p in Path(rel).parts]
    if parts[-1] in RULE_FILE_NAMES:
        return "rule"
    if parts[-1] in PROCEDURE_FILE_NAMES:
        return "procedure"
    if PROCEDURE_DIR in parts[:-1] and Path(rel).suffix.lower() in MARKDOWN_SUFFIXES:
        return "procedure"
    return None


def split_blocks(body: str) -> list[str]:
    """Segment markdown into classification blocks: one block per heading section
    (the heading stays with its body), a heading-less preamble is its own block, and
    any section over MAX_BLOCK_CHARS re-splits on blank lines into paragraph groups.
    Every non-empty character of the body lands in exactly one block."""
    body = body.strip()
    if not body:
        return []
    starts = [m.start() for m in _HEADING_RE.finditer(body)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    sections = [body[a:b].strip() for a, b in zip(starts, starts[1:] + [len(body)])]
    blocks: list[str] = []
    for section in sections:
        if not section:
            continue
        if len(section) <= MAX_BLOCK_CHARS:
            blocks.append(section)
            continue
        group = ""
        for para in re.split(r"\n\s*\n", section):
            para = para.strip()
            if not para:
                continue
            if group and len(group) + len(para) + 2 > MAX_BLOCK_CHARS:
                blocks.append(group)
                group = para
            else:
                group = f"{group}\n\n{para}" if group else para
        if group:
            blocks.append(group)
    return blocks


def _cached_classes(hashes: list[str]) -> dict[str, tuple[str, str]]:
    """hash -> (class, origin) for every kb_block_class row among `hashes`."""
    if not hashes:
        return {}
    with SyncSession() as db:
        rows = db.execute(select(KbBlockClass).where(
            KbBlockClass.content_hash.in_(hashes))).scalars().all()
        return {r.content_hash: (r.content_class, r.origin) for r in rows}


def _store_llm_classes(verdicts: dict[str, str]) -> None:
    """Cache LLM verdicts (origin='llm'). Never touches override rows: the caller
    only submits hashes that had no cache entry at all."""
    if not verdicts:
        return
    with SyncSession() as db:
        existing = {r.content_hash for r in db.execute(select(KbBlockClass).where(
            KbBlockClass.content_hash.in_(list(verdicts)))).scalars().all()}
        for h, cls in verdicts.items():
            if h not in existing:
                db.add(KbBlockClass(content_hash=h, content_class=cls, origin="llm"))
        db.commit()


def _llm_classify(blocks: list[str]) -> list[str] | None:
    """One classifier call for a file's unresolved blocks. Returns one class per
    block, or None on any failure (caller degrades to fact for this run). Unbilled:
    usage is deliberately discarded - classifying the owner's own KB is platform
    cost, exactly like ingest embedding."""
    numbered = "\n\n".join(f"[BLOCK {i}]\n{b[:_LLM_BLOCK_CHARS]}"
                           for i, b in enumerate(blocks))
    try:
        result, _usage = chat_json(
            [{"role": "system", "content": _load_prompt("kb_classifier.md")},
             {"role": "user", "content": numbered}],
            max_tokens=1024)
        classes = result.get("classes")
        if (isinstance(classes, list) and len(classes) == len(blocks)
                and all(c in CLASSES for c in classes)):
            return [str(c) for c in classes]
        log.warning("kb_classify: malformed classifier output (%r); "
                    "defaulting file to fact", classes)
    except Exception as exc:
        log.warning("kb_classify: classifier unavailable (%s); "
                    "defaulting file to fact this run", exc)
    return None


def classify_file(rel: str, text: str) -> tuple[list[tuple[str, str, str]], bool]:
    """The ingest entrypoint: ((block_text, class, content_hash) triples covering
    the file, degraded flag). Non-markdown files are a single fact block (code and
    config inform; they are never rules). Markdown: frontmatter is stripped, a
    deterministic file signal classifies every block, otherwise cached verdicts fill
    in and one LLM call covers the remainder. Admin overrides (origin='override')
    beat everything, including the file signal - overriding IS the escape hatch when
    a signal or the model errs. The flag is True when any block fell back to `fact`
    WITHOUT a real verdict (classifier failure, or blocks past the per-call cap):
    the caller must not let such a run replace digest/procedure state."""
    if Path(rel).suffix.lower() not in MARKDOWN_SUFFIXES:
        return [(text, "fact", norm_hash(text))], False
    meta, body = parse_frontmatter(text)
    # The frontmatter itself stays retrievable as a fact block (a description: or
    # resource: URL is exactly the kind of content retrieval should surface), but it
    # never joins the rule/procedure tiers - it is metadata about the file.
    fm_blocks: list[tuple[str, str, str]] = []
    if meta:
        raw_fm = text[:len(text) - len(body)].strip()
        if raw_fm:
            fm_blocks = [(raw_fm, "fact", norm_hash(raw_fm))]
    blocks = split_blocks(body)
    if not blocks:
        return fm_blocks, False
    hashes = [norm_hash(b) for b in blocks]
    cached = _cached_classes(hashes)
    signal = file_signal(rel, meta)

    resolved: dict[int, str] = {}
    for i, h in enumerate(hashes):
        if h in cached and cached[h][1] == "override":
            resolved[i] = cached[h][0]
        elif signal is not None:
            resolved[i] = signal
        elif h in cached:
            resolved[i] = cached[h][0]

    degraded = False
    pending = [i for i in range(len(blocks)) if i not in resolved]
    if pending:
        ask = pending[:_LLM_MAX_BLOCKS]
        verdicts = _llm_classify([blocks[i] for i in ask])
        if verdicts is not None:
            _store_llm_classes({hashes[i]: v for i, v in zip(ask, verdicts)
                                if hashes[i] not in cached})
            for i, v in zip(ask, verdicts):
                resolved[i] = v
        degraded = verdicts is None or len(pending) > _LLM_MAX_BLOCKS
        for i in pending:
            resolved.setdefault(i, "fact")

    return (fm_blocks + [(blocks[i], resolved[i], hashes[i])
                         for i in range(len(blocks))], degraded)


def compile_digests(rule_blocks: dict[str, list[tuple[str, str, str]]],
                    ) -> tuple[dict[str, str], set[str]]:
    """Compile each root's standing-rules digest from its rule blocks (root_key ->
    [(rel, block_text, content_hash)], file order). Returns (root_key -> digest
    text, demoted content hashes). The budget (settings.kb_rules_digest_max_chars)
    bounds each digest; blocks past it are DEMOTED to the procedure tier - reported
    back so the index carries their real class - never silently truncated. Blocks
    are grouped under one `### <rel>` header per file."""
    digests: dict[str, str] = {}
    demoted: set[str] = set()
    budget = settings.kb_rules_digest_max_chars
    for root_key, blocks in rule_blocks.items():
        parts: list[str] = []
        used = 0
        current_file = None
        root_demoted = 0
        for rel, block_text, content_hash in blocks:
            cost = len(block_text) + (len(rel) + 6 if rel != current_file else 0)
            if used + cost > budget:
                demoted.add(content_hash)
                root_demoted += 1
                continue
            if rel != current_file:
                parts.append(f"### {rel}")
                current_file = rel
            parts.append(block_text)
            used += cost
        if parts:
            digests[root_key] = "\n\n".join(parts)
        if root_demoted:
            log.warning("kb_classify: %d rule block(s) of root %s exceeded the "
                        "digest budget (%d chars) and were demoted to procedure",
                        root_demoted, root_key, budget)
    return digests, demoted
