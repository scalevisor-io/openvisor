"""White-label brand helpers. All user-facing brand strings flow through here:
email subjects, prompt files and static_data are rendered with the placeholders
below so a spoke deployment rebrands via env vars only (BRAND_NAME,
CONSULTANT_NAME - see core/config.py for the defaults)."""
from app.core.config import settings

_PLACEHOLDERS = {
    "{{BRAND_NAME}}": lambda: settings.brand_name,
    "{{CONSULTANT_NAME}}": lambda: settings.consultant_name,
    "{{CONSULTANT_FIRST_NAME}}": lambda: settings.consultant_first_name,
    "{{CONSULTANT_FOCUS}}": lambda: settings.consultant_focus,
    # Domain references (demo subdomains) are NOT the brand name: a brand can be
    # "Acme AI" while demos live on *.acme.example.
    "{{DEPLOY_DOMAIN}}": lambda: settings.deploy_domain,
}


def subject(text: str) -> str:
    """Email subject with the brand prefix: "[<brand>] <text>"."""
    return f"[{settings.brand_name}] {text}"


def render(text: str) -> str:
    """Substitute brand placeholders in a prompt/template string."""
    for key, value in _PLACEHOLDERS.items():
        if key in text:
            text = text.replace(key, value())
    return text


def render_obj(obj):
    """Recursively substitute brand placeholders in parsed JSON/YAML data."""
    if isinstance(obj, str):
        return render(obj)
    if isinstance(obj, list):
        return [render_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v) for k, v in obj.items()}
    return obj
