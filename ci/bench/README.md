# Harness benchmark

Drives the frozen `agent_eval` corpus as REAL builds, once per harness, so two harnesses can be compared on the numbers the business actually bills on.

`agent_eval` already had every piece except this one: a frozen corpus, a `DevRunRecord` captured per dev-run outcome, deterministic gate metrics and a report aggregator. Its CLI said what was missing - "Driving the corpus as builds is a separate, stack-dependent concern." `drive.py` is that piece.

## What it does

For each (spec, harness, repetition) it walks the CUSTOMER path over HTTP - signup, deposit with a connected repo, onboarding answers, evaluation, submit, admin pricing, credit grant, build - and pins the arm's harness and model endpoint **before** anything can dispatch. A benchmark that shortcuts the intake measures a pipeline nobody runs, so it does not.

It computes no metrics. It writes a manifest; `python -m app.services.agent_eval compare <manifest>` joins that to the records the pipeline captured on its own, so benchmark builds and production builds are scored by exactly the same code.

## Running it

Needs the e2e git server (the customer's repo) and one `ModelEndpoint` per arm, created on the admin Model configuration page:

```bash
docker compose -f compose.base.yml -f compose.dev.yml -f ci/compose.e2e.yml up -d gitserver

E2E_BASE=http://127.0.0.1:8090 E2E_APP_HOST=app.<domain> E2E_MAIL_HOST=mail.<domain> \
ADMIN_EMAIL=... ADMIN_PASSWORD=... \
E2E_REPO_SSH_URI=ssh://git@gitserver/srv/git/todo.git \
E2E_INSTALL_KEY_CMD="sh ci/e2e/hooks/install-key.sh" \
python3 ci/bench/drive.py \
  --harness openhands --harness claude_sdk \
  --specs webapp-url-shortener --reps 1 \
  --endpoint openhands="<endpoint label>" \
  --endpoint claude_sdk="<endpoint label>" \
  --out /tmp/bench-manifest.json

docker compose -f compose.base.yml -f compose.dev.yml exec api \
  python -m app.services.agent_eval compare /tmp/bench-manifest.json
```

`--specs all` runs the whole corpus. Every build spends real tokens: price one arm with a single spec before committing to a sweep.

## Reading the result

- **Cost per passing build is the headline**, not pass rate. A harness that buys +3pp quality for +80% tokens is a business loss; the report puts credits first for that reason.
- **pass@1 and pass@k are separate.** The Resume button lets a spec pass on a later attempt, and conflating them hides cost shifted onto the customer.
- **Check the harness_version line.** An arm carrying more than one is not a single configuration and the report says so. A prompt edit or a cap change mid-sweep silently makes the halves incomparable - that is what the fingerprint is for.
- **A failed build is data.** The driver records it rather than retrying, because dropping failures inflates every rate in the table.

## Statistical honesty

13 specs is 13 Bernoulli trials per arm. A 2-of-13 difference is noise. Budget at least 3 repetitions per spec per arm before believing a pass-rate delta, and run the same harness against itself first to measure the floor - a null A/B is the cheapest way to learn how big a difference has to be before it means anything.

## Known cost caveat

Neither price table row encodes prompt-cache WRITES (Anthropic bills 1.25x base input for a 5-minute write; `gpt-5.6-terra` bills 5.00/M). A measured Claude Sonnet 5 run reported $0.066993 to the provider against $0.0557 computed from the table - a 17% under-bill, entirely that gap. Credits figures are therefore a lower bound, and the gap is larger for whichever harness re-primes a bigger prefix. `run_claude.py` writes the provider's own `total_cost_usd` into `usage.json` as `provider_cost_usd`; use it as ground truth until a `cache_write` column exists.

## Pricing a sweep before you run one

Run one spec on one arm first and read the credits and wall clock off the report. Per-build cost and duration vary by an order of magnitude across engines, models and specs, so multiply your own measured figure rather than any number written here: the corpus at N repetitions is `specs x arms x N` builds, all of them spending real tokens.

## Blocking defect: the pass gate does not fit every provider path

A build that pushes a branch successfully can still score **pass@1 = 0** and land in the failure histogram as `infra-error`.

`metrics._PASS_STATES` is `("done", "awaiting_merge", "deploying")`. On the plain-push provider path the runner pushes, the project is handed back to the customer, and `dev_run_state` resets to `idle` - which is not in that tuple. Every build on that path is therefore scored as a failure, and a comparison run today reports a dead heat at zero.

This is not a benchmark-only bug: `capture_run_record` writes the same `final_state` for production builds, so the live `report --db` numbers understate real pass rates on that path too.

Fix before trusting any pass-rate column: decide whether `idle` reached from `development` with a pushed branch is a pass, and encode that in `metrics.is_pass` rather than in the driver. Cost and wall-clock columns are unaffected - they come from the runner's own metering.

## Holding the model fixed

An engine comparison is only a comparison if the model is the same on both arms. Pass the SAME `--endpoint` label for every `--harness` unless you deliberately want a combined engine-and-model result, and check the report's header line: it prints the models and the harness versions per arm, and flags an arm carrying more than one configuration.

Note that not every engine is provider-agnostic. One that speaks a single vendor's protocol constrains which models the comparison can hold fixed, so pick the model the constrained arm can run and point the flexible arm at it.
