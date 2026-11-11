"""The MCP sidecars the dev agent calls during a build, in one image.

One app, one route per sidecar (`POST /<name>/mcp`), one Deployment. That is the
point of the shape: adding a sidecar is a module plus a `mount()` line, with no
compose service, chart Deployment, NetworkPolicy rule or image-push list to
touch - the four wirings that a separate image costs every time.

Not everything belongs here. A sidecar that needs a different base image or a
licence boundary of its own gets its own directory and image instead (donsetch/,
whose engine needs glibc 2.38+ and is AGPL-3.0), and the public MCP server that
customers connect to is a different product surface entirely (mcp/main.py, on
mcp.<domain>).

Never name a module after a stdlib package: this directory is the first entry on
the import path, so an `email.py` here would replace the standard `email` that
http.client imports. Hence `email_tool.py` serving the `/email` route.
"""
from fastapi import FastAPI

import email_tool

app = FastAPI(title="sidecars-mcp")

SIDECARS = {"email": email_tool.router}

for name, router in SIDECARS.items():
    app.include_router(router, prefix=f"/{name}")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "sidecars": sorted(SIDECARS)}
