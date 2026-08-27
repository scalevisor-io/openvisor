# DonSeTch web-research sidecar

The dev agent's web research tools - `web_search`, `web_fetch`, `web_crawl` - served
to a build over MCP. [`bridge.py`](bridge.py) adapts the engine's stdio JSON-RPC to the
streamable-HTTP URL every other MCP server on this platform is addressed by, and gates
each tool on the capabilities the admin left enabled on the §Tools row.

See [docs/CODE_MAP.md](../docs/CODE_MAP.md#web-research-donsetch) for how a run reaches it.

## Licensing

The engine is [DonSeTch](https://github.com/dondai44423/donsetch), **AGPL-3.0-only**.
This platform is Apache-2.0. The two stay separable:

- The binary is used **unmodified**, exactly as published in the upstream project's
  GitHub release, and is checksum-verified at image build.
- It runs as a **separate executable behind a process boundary** - the bridge speaks
  JSON-RPC to it over a pipe. Nothing is linked, and no AGPL source is vendored into
  this repository.
- The image built from this directory **conveys** that binary, so it carries the
  engine's own licence and this notice. Complete corresponding source for the exact
  version is the upstream tag: `https://github.com/dondai44423/donsetch/tree/v<version>`
  (`DONSETCH_VERSION` in the [Dockerfile](Dockerfile)).

Modifying the engine and deploying the result would put AGPL §13 obligations on the
operator. Don't fork it here - bump the pinned version instead.

## Bumping the version

`DONSETCH_VERSION` and `DONSETCH_SHA256` in the Dockerfile move together; the checksum
is the release's published `donsetch-linux-x64.tar.gz.sha256`. Re-run the sidecar tests
after a bump - the tool set is pinned in `TOOL_CAPS` and a release that adds a fourth
tool must be reviewed before a run can call it.
