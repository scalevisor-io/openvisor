"""Agent-mention detection, shared by the API dispatch path (project_actions)
and the workers. Addressing the agent directly is an explicit demand for a
reply, so a mention is never met with silence (§work answers, and the chat-kind
responder's admin-summons path)."""
import re

# The lookbehind keeps emails and paths (a@ai.com, docs/@ai) from matching.
_AGENT_MENTION_RE = re.compile(r"(?<![\w./@-])@(?:agent|ai)\b", re.I)


def mentions_agent(text: str | None) -> bool:
    return bool(text and _AGENT_MENTION_RE.search(text))
