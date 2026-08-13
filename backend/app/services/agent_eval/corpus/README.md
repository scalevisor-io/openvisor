# Eval corpus

Frozen project specs that drive the agent eval, stratified by speciality.

**These are AUTHORED, not mined from production.** A fresh deployment has no delivered customer
projects, so there is no real corpus to source from. Author realistic specs and
freeze them; replace/augment with real (anonymised) customer specs as they arrive —
real specs nobody trained on are far better evidence than any public benchmark.

## Schema (one JSON file per spec)

| field | required | meaning |
|---|---|---|
| `id` | ✓ | unique, stable, kebab-case (also the filename) |
| `speciality` | ✓ | a valid id from `static_data/specialities.json` |
| `description` | ✓ | the frozen project brief the agent receives |
| `from_scratch` | | greenfield (default `true`) vs an existing repo |
| `sovereign` | | the sovereign toggle (default `false`) |
| `onboarding_answers` | | list of `{q, a}` — the onboarding step-2 answers |
| `deliverable_type` | | `deployed_demo` (default), `audit_report`, `architecture_docs` |
| `notes` | | free text (not sent to the agent) |

Freeze the spec text once set — changing it breaks run-to-run comparability. Grow this
to ~20 (Anthropic's rule: ~20 real-usage cases is enough to see change and few enough
to actually run), keeping the per-speciality stratification.

Validate: `python -m app.services.agent_eval validate`.
