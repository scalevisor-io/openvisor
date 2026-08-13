<!-- prompt: acceptance_checks | version: 1 -->
You write a SMALL set of deterministic acceptance checks for a web app that {{BRAND_NAME}}'s
build agent will produce from the spec below. Each check fetches a URL from the running app
over HTTP and looks for expected literal text in the response body. They prove the app does
what the spec ASKED FOR - the boot gate already proves it starts, these prove it conforms.

Rules:
- Return 2-5 checks, no more. Always include a check for GET "/" (the app's main page). Add a
  few other GET paths ONLY if the spec clearly implies them (e.g. a named API endpoint or page).
- Each check has: "path" (an HTTP path starting with "/", GET only), a "contains" list of 1-3
  SHORT literal text fragments you expect to appear verbatim in that response body, and a short
  "desc".
- Choose "contains" fragments a correct implementation of THE SPEC would almost certainly emit
  and that an empty, placeholder, or wrong app would not: names of asked-for features, UI labels,
  key element ids/classes, a page <title>. Keep each fragment to a word or two, unambiguous.
- Do NOT invent exact strings the spec never implies. When unsure, check only GET "/" for a
  couple of spec keywords. These are a best-effort conformance FILTER, not a gate - prefer FEW,
  HIGH-CONFIDENCE checks over many brittle ones.
- Paths and fragments MUST be plain text: no shell metacharacters, quotes, backticks, angle
  brackets, or newlines. A check that needs any of those is not a good check - drop it.

Respond with ONLY a JSON object, no prose:
{"checks": [{"path": "/", "contains": ["fragment one", "fragment two"], "desc": "what this proves"}]}
An empty checks list is acceptable when the spec gives nothing reliable to assert.
