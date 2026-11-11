"""§Tools `email`: notification email for the dev agent, to approved addresses only.

Unlike a vendor sidecar, this one holds no provider credential, not even per
request. The Bearer a build carries is a run token bound to its project, and
the SMTP settings never leave the platform: every call is relayed to the api's
internal route (`/internal/email-tool/*`, which neither Traefik nor the SPA
forwards), and the api decides - recipient allowlist, per-project quota, the row
still being enabled - before anything reaches SMTP. Handing the sandbox the
SMTP password instead would make the allowlist a suggestion: the agent, or any
code it runs, could talk to the relay directly.

Two tools: `list_recipients` (who this project may write to) and `send_email`.
A refusal comes back as the api's own one-line reason, so the agent can fix the
call instead of guessing.
"""
import os

import httpx

from rpc import ToolError, clip, mcp_router

API_INTERNAL_URL = os.environ.get("API_INTERNAL_URL", "http://api:8000").rstrip("/")
_TIMEOUT = 45.0  # the api allows SMTP 20 s to connect, plus the send itself

TOOLS = [
    {
        "name": "send_email",
        "description": (
            "Email a short plain-text notification. Use it only when the customer "
            "asked to be notified by email, or the task explicitly calls for an "
            "email. Recipients are restricted to the project's customer and the "
            "addresses the administrator approved; list_recipients shows them. "
            "Every message sent is also recorded in the project's chat."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"},
                       "minItems": 1, "maxItems": 10,
                       "description": "Recipient addresses, each one on the approved list."},
                "subject": {"type": "string", "maxLength": 200,
                            "description": "One-line subject."},
                "body": {"type": "string", "maxLength": 20000,
                         "description": "Plain text. Blank lines separate paragraphs, "
                                        "lines starting with '- ' become a list."},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "list_recipients",
        "description": ("The addresses this project may email: the project's "
                        "customer and any the administrator approved."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _recipients_list(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    if not isinstance(raw, list):
        raise ToolError("`to` must be a list of email addresses")
    out = [str(a).strip() for a in raw if str(a).strip()]
    if not out:
        raise ToolError("`to` needs at least one address")
    return out


def _refusal(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) for d in detail[:3])
    if resp.status_code == 429:
        detail = "this project's hourly email quota is used up - try again later"
    return clip(str(detail or f"the platform answered HTTP {resp.status_code}"), 400)


async def dispatch(tool: str, args: dict, key: str | None) -> str:
    if not key:
        raise ToolError("email is not set up for this build - nothing was sent")
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        if tool == "list_recipients":
            resp = await client.get(f"{API_INTERNAL_URL}/internal/email-tool/recipients",
                                    headers=headers)
            if resp.status_code != 200:
                raise ToolError(_refusal(resp))
            rows = resp.json().get("recipients") or []
            if not rows:
                return "No approved recipients for this project."
            return "\n".join(f"- {r['address']} ({r['label']})" for r in rows)
        subject = " ".join(str(args.get("subject") or "").split())
        body = str(args.get("body") or "")
        if not subject or not body.strip():
            raise ToolError("`subject` and `body` are both required")
        resp = await client.post(f"{API_INTERNAL_URL}/internal/email-tool/send",
                                 headers=headers,
                                 json={"to": _recipients_list(args.get("to")),
                                       "subject": subject[:200], "body": body[:20000]})
        if resp.status_code != 200:
            raise ToolError(_refusal(resp))
        return "Sent to " + ", ".join(resp.json().get("sent_to") or []) + "."


router = mcp_router("email", TOOLS, dispatch, max_chars=4000)
