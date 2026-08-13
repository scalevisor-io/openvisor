# Building a {{BRAND}} Program

A Program is a small runnable repository the {{BRAND}} platform executes on demand, on a schedule, or from an inbound webhook. It receives validated customer inputs, does its work inside a throwaway sandbox, and returns text output (plus optional files) that lands in the customer's run history and outcome webhook.

This document is the complete authoring contract. It is written so you can hand it to an AI assistant as-is: paste it together with a description of what your program should do, and ask for a repository that follows every rule below.

## How the platform runs your repository

Every run happens in a fresh Docker-in-Docker sandbox. The platform checks out your repository, writes the run's inputs and secrets next to it, then executes three phases:

1. `docker compose build`
2. a deploy validation (`docker compose config` + container create)
3. `docker compose run --rm program`

Rules that follow from this:

- Build or deploy errors are platform-visible failures. The exit code of phase 3 is yours: `0` marks the run **succeeded**, anything else marks it **failed** (with the exit code shown).
- Everything your program prints to stdout/stderr streams live into the customer's run log. Print progress; never print secrets.
- Runs are killed at the program's timeout (default 15 minutes, admin-configurable). Budget accordingly.
- One run per instance at a time; there is no persistent state between runs except Docker's layer cache. Treat every run as stateless.
- The sandbox is destroyed after every run.

## Repository layout

```
your-program/
├── compose.yml          # REQUIRED - the run contract (see below)
├── Dockerfile           # your image; any language works
├── input.template.yml   # declares the input form customers fill
├── main.py              # your entrypoint (any name/language)
├── README.md            # doubles as the catalog description customers read
├── input/               # platform writes input/input.yml here (mounted read-only)
├── output/              # write your results here (mounted read-write)
├── .openvisor/          # write the billing report here (mounted read-write)
└── secrets/             # platform writes secrets/ssh_key here before each run
```

## compose.yml - copy this contract

```yaml
services:
  program:
    build: .
    network_mode: host
    environment:
      OPENAI_BASE_URL: ${OPENAI_BASE_URL:-}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      OPENAI_MODEL: ${OPENAI_MODEL:-}
      EMBEDDING_BASE_URL: ${EMBEDDING_BASE_URL:-}
      EMBEDDING_API_KEY: ${EMBEDDING_API_KEY:-}
      EMBEDDING_MODEL: ${EMBEDDING_MODEL:-}
      CONTEXT7_MCP_URL: ${CONTEXT7_MCP_URL:-}
      CONTEXT7_API_KEY: ${CONTEXT7_API_KEY:-}
    volumes:
      - ./input:/app/input:ro
      - ./output:/app/output
      - ./.openvisor:/app/.openvisor
    secrets:
      - ssh_key
    mem_reservation: "${PROGRAM_MEM_RESERVATION:-256m}"
    mem_limit: "${PROGRAM_MEM_LIMIT:-1g}"
    cpus: "${PROGRAM_CPUS:-1}"
    logging:
      options:
        max-size: "1m"

secrets:
  ssh_key:
    file: ./secrets/ssh_key
```

Non-negotiable rules:

- The runnable service MUST be named `program`.
- Keep `network_mode: host` (platform hostnames resolve from the sandbox) and the `logging.options.max-size: "1m"` cap.
- Resource keys use `mem_reservation` / `mem_limit` / `cpus` fed by the platform-injected `PROGRAM_*` variables; the `:-` defaults keep local `docker compose run program` working.
- Keep the `ssh_key` compose secret declared even if you do not use SSH - the platform writes the file before every run.

## input.template.yml - the customer input form

The platform generates a form from this file and validates every field deterministically before the run starts (a wrong input names exactly which field failed). Omit the file if your program takes no inputs.

```yaml
inputs:
  - name: repo_url          # required; [a-zA-Z_][a-zA-Z0-9_]*, unique; the key in input/input.yml
    label: Repository URL   # optional; defaults to the name
    description: Shown under the field.
    type: text              # text | multiline | number | boolean | choice (default text)
    required: true          # default false
    placeholder: https://example.com
  - name: mode
    type: choice
    options: [fast, thorough]   # choice type only; non-empty list
    default: fast               # used when the customer leaves the field empty
  - name: api_token
    type: text
    secret: true                # masks the value in the UI (display only)
```

## Runtime contract - paths inside the container

| Path | Direction | Meaning |
|---|---|---|
| `/app/input/input.yml` | read | The customer's inputs as a flat YAML mapping (`name: value`), already validated. |
| `/app/input/event.json` | read | Only on webhook-triggered runs: the normalized inbound event (see Triggers). Ignore it if you don't use hooks. |
| `/app/output/output.txt` | write | The text shown to the customer and sent to the outcome webhook. Always write it. |
| `/app/output/*` | write | Any extra files; all of them are listable and downloadable from the run history. Files written anywhere else are lost. |
| `/app/.openvisor/usage.json` | write | The LLM billing report (see Billing). |
| `/run/secrets/ssh_key` | read | The instance's SSH PRIVATE key. The matching public key is shown to the customer so they can authorize it wherever the program must connect. Never copy it into output or logs. |

## Environment variables

| Variable | Meaning |
|---|---|
| `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` | The platform LLM (OpenAI-compatible chat-completions endpoint), resolved per run: the model the customer pinned on this instance if there is one, else the program's default. Use these for every LLM call; never ship your own model keys and never hardcode a model name - `OPENAI_MODEL` is what the run is billed against. |
| `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` | The platform embedding endpoint. |
| `CONTEXT7_MCP_URL`, `CONTEXT7_API_KEY` | The Context7 documentation MCP, when enabled on the platform. |
| `PROGRAM_CPUS`, `PROGRAM_MEM_LIMIT`, `PROGRAM_MEM_RESERVATION` | Admin-set resources, interpolated into `compose.yml` - do not read them from code. |

All are empty when you run locally unless you export them yourself.

## Billing - .openvisor/usage.json

If your program calls an LLM, write the total consumption before exiting:

```json
{"model": "<the OPENAI_MODEL env value, verbatim>", "input_tokens": 1234, "output_tokens": 567, "cached_input_tokens": 800}
```

- Report the CONFIGURED model name (the `OPENAI_MODEL` value), not a provider-side alias - the platform prices by that name and an unknown model fails the billing loudly.
- `cached_input_tokens` is optional: the input tokens the provider reported as prompt-cache reads (`usage.prompt_tokens_details.cached_tokens` on OpenAI-compatible APIs, a subset of `input_tokens`). Report it when available - cached reads are billed at the model's discounted cached rate; omitted, every input token bills at the full rate.
- Several LLM calls in one run: sum the tokens into one report.
- No report = nothing billed. Never fabricate token counts.

## Triggers

- **Manual** - the customer hits Run on the instance.
- **Schedule** - cron presets, floor of 15 minutes between runs; scheduled ticks skip while a run is in flight.
- **Inbound webhook** - the customer can point a signed GitHub/GitLab issue-event webhook at their instance. The normalized event arrives as `/app/input/event.json`: `{"provider": "github"|"gitlab", "event": "issues", "action": "...", "delivery": "...", "issue": {"iid", "url", "title", "body", "labels", "assignees", "author"}}`. Programs that don't care simply ignore the file.

At the end of EVERY run the platform POSTs the outcome (state, exit code, error, the `output.txt` text, credits charged) to the instance's webhook URI if the customer configured one - so `output.txt` should be self-contained and useful to downstream tooling.

## Confidentiality

Everything the customer would see (output files AND the run log) is leak-scanned by the platform before it is released: private-key material, platform secret values and knowledge-base text all block the run and withhold the output. Practical rules:

- Never print or write API keys, tokens or the SSH key - not even partially.
- Treat customer-provided secrets (declared with `secret: true`) the same way: use them in requests, keep them out of stdout and `output/`.

## Develop and test locally

```bash
ssh-keygen -t ed25519 -f secrets/ssh_key -N ""    # throwaway key, never commit one
printf 'repo_url: https://example.com\n' > input/input.yml
export OPENAI_BASE_URL=... OPENAI_API_KEY=... OPENAI_MODEL=...   # your own dev creds
docker compose run --rm program
cat output/output.txt
```

Add a `.gitignore` so run artifacts and keys never land in git:

```
secrets/ssh_key
secrets/ssh_key.pub
input/input.yml
input/event.json
output/*
!output/.gitkeep
.openvisor/*
!.openvisor/.gitkeep
```

## Worked example - a minimal LLM program

`main.py` (with `pyyaml` and `httpx` installed in the Dockerfile):

```python
import json, os, sys
from pathlib import Path
import httpx, yaml

INPUT = Path("/app/input/input.yml")
OUTPUT = Path("/app/output/output.txt")
USAGE = Path("/app/.openvisor/usage.json")

def main() -> int:
    inputs = yaml.safe_load(INPUT.read_text()) if INPUT.is_file() else {}
    topic = str((inputs or {}).get("topic") or "").strip()
    if not topic:
        print("input error: 'topic' is required", file=sys.stderr)
        return 2
    base, key, model = (os.environ.get(k, "") for k in
                        ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"))
    if not (base and key and model):
        print("platform error: OPENAI_* env not provided", file=sys.stderr)
        return 1
    resp = httpx.post(f"{base.rstrip('/')}/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "messages": [
                          {"role": "user", "content": f"Three bullet points about {topic}."}]},
                      timeout=120)
    resp.raise_for_status()
    data = resp.json()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(data["choices"][0]["message"]["content"].strip() + "\n")
    usage = data.get("usage") or {}
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps({"model": model,
                                 "input_tokens": usage.get("prompt_tokens", 0),
                                 "output_tokens": usage.get("completion_tokens", 0)}))
    print(f"Done: reviewed '{topic}'.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

`Dockerfile`:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir pyyaml httpx
WORKDIR /app
COPY main.py .
CMD ["python", "main.py"]
```

## Quality checklist before you hand it over

- [ ] `docker compose build` and `docker compose run --rm program` work locally with a throwaway SSH key and a sample `input/input.yml`.
- [ ] The service is named `program`; the compose contract above is intact.
- [ ] `output/output.txt` is written on every path, including errors (a clear one-line failure message beats an empty output).
- [ ] Missing/invalid inputs exit non-zero with a clear message on stderr.
- [ ] `usage.json` reports the `OPENAI_MODEL` value verbatim and sums all calls.
- [ ] No secret ever reaches stdout, stderr or `output/`.
- [ ] `README.md` describes the program for customers - it becomes the catalog page.

## Publishing

Programs run from the {{BRAND}} platform's private GitLab, which is not publicly writable: send your finished repository (a git URL or an archive) to your {{BRAND}} contact. They import it, run the platform's automated check (build + deploy validation), and publish it to the catalog - where you and your team can add instances, schedule runs and wire webhooks.
