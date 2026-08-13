"""The eval corpus - frozen project specs, stratified by speciality.

IMPORTANT: a fresh deployment has no delivered customer projects, so this corpus
is AUTHORED, not mined from production. Each spec is a frozen
(speciality + flags + description + onboarding answers) tuple that drives one build.
Freeze the spec text so a score is comparable run to run; version the corpus in git.
Specs are JSON (matching static_data), so no new dependency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
_REQUIRED = ("id", "speciality", "description")


@dataclass(frozen=True)
class EvalSpec:
    id: str
    speciality: str
    description: str
    from_scratch: bool = True
    sovereign: bool = False
    onboarding_answers: tuple = ()          # tuple of (q, a) - immutable/hashable
    deliverable_type: str = "deployed_demo"
    notes: str = ""


def _load_one(path: Path) -> EvalSpec:
    data = json.loads(path.read_text())
    missing = [k for k in _REQUIRED if not str(data.get(k, "")).strip()]
    if missing:
        raise ValueError(f"{path.name}: missing required field(s): {', '.join(missing)}")
    raw = data.get("onboarding_answers") or []
    if isinstance(raw, dict):
        answers = tuple(sorted((str(k), str(v)) for k, v in raw.items()))
    else:
        answers = tuple(
            (str(a.get("q", "")), str(a.get("a", ""))) if isinstance(a, dict) else (str(a), "")
            for a in raw
        )
    return EvalSpec(
        id=str(data["id"]),
        speciality=str(data["speciality"]),
        description=str(data["description"]).strip(),
        from_scratch=bool(data.get("from_scratch", True)),
        sovereign=bool(data.get("sovereign", False)),
        onboarding_answers=answers,
        deliverable_type=str(data.get("deliverable_type", "deployed_demo")),
        notes=str(data.get("notes", "")),
    )


def load_corpus(corpus_dir: Path | None = None) -> list[EvalSpec]:
    d = corpus_dir or CORPUS_DIR
    specs = [_load_one(p) for p in sorted(d.glob("*.json"))]
    ids = [s.id for s in specs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate spec id(s): {', '.join(dupes)}")
    return specs


def stratify(specs: list[EvalSpec]) -> dict[str, list[EvalSpec]]:
    out: dict[str, list[EvalSpec]] = {}
    for s in specs:
        out.setdefault(s.speciality, []).append(s)
    return out
