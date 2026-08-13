"""Project naming (§9.2). The customer no longer types a project name: a
bootstrap name is derived from the description at creation, then the LLM
title-generation prompt (#9) refines it during evaluation unless the customer
has renamed the project (project.name_customized)."""
import re


def name_from_description(description: str) -> str:
    """Deterministic bootstrap name: the first words of the description.
    Good enough for the seconds between creation and the LLM title pass,
    and the fallback when the model endpoint is unavailable."""
    words = re.findall(r"[\w'-]+", description)
    name = " ".join(words[:8])[:60].strip() or "Untitled project"
    return name[0].upper() + name[1:]


def subdomain_for(project_id: str, name: str) -> str:
    """`<uuid-prefix>-<slug>` demo subdomain, slug derived from the name."""
    uuid_prefix = project_id.split("-")[0]
    slug = "".join(c for c in name.lower().replace(" ", "-") if c.isalnum() or c == "-")[:20]
    return f"{uuid_prefix}-{slug}".strip("-")


def sanitize_branch(name: str | None) -> str | None:
    """Git-ref-safe branch name or None when unusable. Keeps the model's casing
    and separators (customer conventions like `f/#-` carry meaning) while
    refusing anything git would reject or that collides with a base branch."""
    s = (name or "").strip().strip('"').strip("'")
    s = re.sub(r"\s+", "-", s)
    s = "".join(c for c in s if c.isalnum() or c in "/_#.-")
    s = re.sub(r"/{2,}", "/", s).strip("/-.")
    if ".." in s or "@{" in s or s.endswith(".lock"):
        return None
    if len(s) > 60:
        s = s[:60].rstrip("/-.")
    if len(s) < 3 or not any(c.isalnum() for c in s):
        return None
    if s.lower() in ("main", "master", "head", "develop", "agent"):
        return None
    return s
