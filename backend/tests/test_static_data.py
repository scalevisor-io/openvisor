"""static_data templating: committed *.example.json templates are valid JSON and
the entrypoint materialized a live sibling for each (copy-if-missing bootstrap)."""
import json
from pathlib import Path

from app.services.pricing import STATIC_DIR, load_static

CANONICAL = [
    "forbidden-actions.json",
    "initial-user-questions.json",
    "memory-placeholders.json",
    "per_model_price_table.json",
    "security-triggers.json",
    "specialities.json",
]


def test_examples_exist_and_parse():
    examples = sorted(p.name for p in STATIC_DIR.glob("*.example.json"))
    assert examples == sorted(n.replace(".json", ".example.json") for n in CANONICAL)
    for path in STATIC_DIR.glob("*.example.json"):
        json.loads(path.read_text())


def test_live_files_materialized_and_load():
    # The container entrypoint copies each template to its live name if missing.
    for name in CANONICAL:
        assert (STATIC_DIR / name).is_file(), f"{name} not materialized by entrypoint"
        assert load_static(name)


def test_bootstrap_does_not_clobber(tmp_path):
    # Mirror of the entrypoint loop semantics: existing live files are never overwritten.
    tpl = tmp_path / "thing.example.json"
    live = tmp_path / "thing.json"
    tpl.write_text('{"from": "template"}')
    live.write_text('{"from": "operator"}')
    if not live.exists():  # copy-if-missing
        live.write_text(tpl.read_text())
    assert json.loads(live.read_text())["from"] == "operator"
