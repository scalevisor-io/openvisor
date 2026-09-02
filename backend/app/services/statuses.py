"""Project status machine (PROMPT §8). Pure transition rules shared by the
async API path and the sync Celery path."""
from dataclasses import dataclass

STATUSES = {
    "draft", "awaiting_review", "payment_due", "development",
    "awaiting_customer", "awaiting_admin", "finished", "canceled",
}

# Reader-facing names for the same statuses. The wire format never changes -
# this is what a subject line and an email body say instead of "payment_due".
STATUS_LABELS = {
    "draft": "Draft",
    "awaiting_review": "Awaiting review",
    "payment_due": "Payment due",
    "development": "In development",
    "awaiting_customer": "Awaiting the customer",
    "awaiting_admin": "Awaiting the consultant",
    "finished": "Finished",
    "canceled": "Canceled",
}


def status_label(status: str) -> str:
    """The reader-facing name, falling back to the raw status."""
    return STATUS_LABELS.get(status, status)


# from -> {to: allowed actors}
TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "draft": {
        "awaiting_review": {"customer", "admin"},
        "canceled": {"system", "admin"},
    },
    "awaiting_review": {
        "awaiting_customer": {"admin"},
        "payment_due": {"admin"},
        "canceled": {"admin", "system"},
    },
    "payment_due": {
        "development": {"system", "admin"},
        "canceled": {"system", "admin"},
    },
    "development": {
        "awaiting_customer": {"agent", "admin"},
        "awaiting_admin": {"agent", "customer", "admin"},
        "finished": {"admin"},
        "canceled": {"admin"},
    },
    "awaiting_customer": {
        "development": {"customer", "system", "admin"},
        "awaiting_admin": {"customer", "agent", "admin"},
        # The customer accepts the delivered MVP once the demo is live (§ delivery
        # acceptance). The agent still can never advance to finished.
        "finished": {"customer", "admin"},
        "canceled": {"admin"},
    },
    "awaiting_admin": {
        "development": {"admin"},
        "awaiting_customer": {"admin"},
        "awaiting_review": {"admin"},
        "payment_due": {"admin"},
        "finished": {"admin"},
        "canceled": {"admin"},
    },
    "finished": {"development": {"admin"}, "awaiting_admin": {"customer", "admin"}},
    "canceled": {"draft": {"admin"}},
}

# any -> awaiting_admin via the customer request-review button
ALWAYS_ALLOWED = {("awaiting_admin", "customer"), }


@dataclass
class EmailPlan:
    to_admin: bool = False
    to_customer: bool = False


def emails_for(from_status: str | None, to_status: str) -> EmailPlan:
    """§8 table: emails on awaiting_admin/awaiting_review entry + customer-facing
    transitions."""
    if to_status == "awaiting_admin":
        return EmailPlan(to_admin=True)
    if to_status == "awaiting_review":
        return EmailPlan(to_admin=True, to_customer=True)
    if to_status == "canceled":
        return EmailPlan(to_admin=True, to_customer=True)
    if to_status == "awaiting_customer":
        return EmailPlan(to_customer=True)
    if to_status == "payment_due":
        # Always tell the customer payment is due, regardless of the prior state
        # (a project can reach payment_due from awaiting_review or awaiting_admin).
        return EmailPlan(to_customer=True)
    if to_status == "finished":
        return EmailPlan(to_customer=True)
    return EmailPlan()


def can_transition(from_status: str, to_status: str, actor: str) -> bool:
    if actor == "admin" and to_status in STATUSES:
        return True  # admin can move to any status manually (§8)
    if to_status == "awaiting_admin" and actor in ("customer", "agent") and from_status != "canceled":
        return True  # customer request-review button / agent defer (§12)
    allowed = TRANSITIONS.get(from_status, {})
    return actor in allowed.get(to_status, set())
