"""Agent-quality evaluation harness (Phase 0).

The blocker the harness R&D identified: we have ~200 platform tests and ZERO
agent-quality evaluation, so a harness change and a model change are indistinguishable
after the fact (same-model harness spreads run ~24 points). This module is the
foundation that makes every subsequent harness decision measurable instead of a guess.

It is deliberately PURE and offline: a corpus of frozen specs, a stable harness-version
fingerprint, deterministic run metrics + failure triage, and a report aggregator whose
headline is COST PER PASSING BUILD (not resolve rate - our objective function is not the
literature's). Driving real builds against the corpus is a separate, stack-dependent
concern; these pieces turn the run records it produces into decisions.
"""
