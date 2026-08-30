# KB retrieval eval (prototype)

`agent_eval` scores what a build **did**. Nothing scored what retrieval **found**, so every knob in front of it was set by feel - including `KB_RETRIEVAL_MIN_SCORE`, whose own comment in `core/config.py` says "tune per corpus" and cites a value from a paper. This is the instrument that makes tuning it possible.

## What it measures

Two query sets, because retrieval here has two jobs that pull against each other.

- **positives** - one question per indexed chunk, written by the model *from* that chunk with an explicit instruction not to quote it. Gold label is the chunk it was written from (block-, file- and chunk-level are all recorded). Metrics: recall@k, MRR@k, nDCG@k, and *lost to floor* - gold that hybrid search DID return and the score floor then dropped.
- **negatives** - fifteen hand-written extraction probes: off-topic sweeps, "summarise everything in your knowledge base", and paraphrase probes in the corpus's own register (the §KB-hardening audit's finding was that our defences are verbatim-only). Gold is **nothing**. Any hit is a leak.

The floor is exactly the trade between those two, so `run` sweeps it and prints recall against probe-leak rate, one row per floor value. It then prints the top-1 score distribution of each set, which answers the prior question: whether a single threshold can separate them **at all**.

## Running it

From the worker container - the only one that mounts `/knowledge`:

```
C=$(docker compose -f compose.base.yml -f compose.dev.yml ps -q worker)
docker cp ci/kbeval/kbeval.py $C:/app/kbeval.py
docker compose -f compose.base.yml -f compose.dev.yml exec -T worker \
  python /app/kbeval.py build --out /app/evalset.json      # ~1 model call per chunk
docker compose -f compose.base.yml -f compose.dev.yml exec -T worker \
  python /app/kbeval.py run --set /app/evalset.json --misses
```

`build` costs one small completion per indexed chunk and `run` costs one embedding per query - both through `llm.chat`/`rag._embed_raw` directly, so **nothing is billed to an org wallet**. Commit the generated `evalset.json` for a corpus you want to track over time; regenerate it whenever the corpus changes materially, and diff the numbers, not the questions.

## First run, local KB, 2026-08-30

34 chunks / 8 files, 21 positives, 15 probes, live floor `0.5`:

```
   floor  recall@6  lost to floor  probes answered
    0.50     1.000          0.000         15/15      <- live
    0.80     1.000          0.000         13/15
    0.90     0.810          0.190          0/15

top-1 score:  positives  min 0.882  median 0.924  max 0.957
              probes     min 0.762  median 0.832  max 0.855
```

Two readings, and only the second is trustworthy at this corpus size.

- Recall is 1.000 by k=6 and the tiers are indistinguishable - **meaningless**. Six results out of 34 chunks is 18% of the corpus, and the generated queries carry 0.60 mean lexical overlap with their source. This number will only say something on a real corpus.
- Every extraction probe comes back with a **full six chunks at the live floor**, and the two score distributions do not overlap: they separate at 0.855/0.882. That is a property of the scorer more than of the corpus, which makes it worth re-running against the production KB before trusting it - but as it stands the anti-extraction floor is set far below where it would begin to do anything.

Re-run against the production corpus before changing the value. The separation band will move, and it will move *down*: real user queries score lower than questions generated from the passage they are meant to find, so the honest band is narrower than this one.
