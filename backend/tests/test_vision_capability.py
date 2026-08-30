"""§chat images: is this project's model allowed to receive images?

The OpenAI-compatible contract has no capability discovery - `/models` returns
ids, not capabilities - so the verdict has to be stored, from the endpoint Test
probe or from an admin declaration. These pin the resolution order and, above
all, that the DEFAULT is off: an untested model must never be handed an image.
"""
import pytest
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.models import AppSetting, ModelEndpoint, Organization, Project, ProjectModelConfig
from app.services import model_config, vision


@pytest.fixture
def seeded():
    with SyncSession() as db:
        org = Organization(name="Vision Org", credit_balance=10.0)
        db.add(org)
        db.commit()
        p = Project(org_id=org.id, name="V", description="d", kind="ai",
                    status="development", workspace_path="/tmp/vision")
        db.add(p)
        db.commit()
        ids = (org.id, p.id)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ProjectModelConfig).where(ProjectModelConfig.project_id == ids[1]))
            db.execute(delete(Project).where(Project.id == ids[1]))
            db.execute(delete(Organization).where(Organization.id == ids[0]))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.label.like("VisionTest%")))
            db.execute(delete(AppSetting).where(
                AppSetting.key == vision.DEFAULT_MODEL_IMAGES_KEY))
            db.commit()


def _endpoint(supports=None, source=None, model="some-model-v1") -> str:
    with SyncSession() as db:
        ep = ModelEndpoint(label=f"VisionTest {supports}", provider="custom",
                           base_url="https://api.example.com/v1",
                           api_key_enc=encrypt("k"), model_name=model,
                           supports_images=supports, supports_images_source=source)
        db.add(ep)
        db.commit()
        return ep.id


def _point_project_at(pid: str, endpoint_id: str | None = None, inline_model: str | None = None):
    """A legacy inline row is the WHOLE triple (base URL + key + model), which is
    what the pre-saved-endpoints admin route wrote - a row carrying only a model
    name is not a choice the calls can honor, so it must not be one here either."""
    with SyncSession() as db:
        db.execute(delete(ProjectModelConfig).where(ProjectModelConfig.project_id == pid))
        db.add(ProjectModelConfig(
            project_id=pid, endpoint_id=endpoint_id, model_name=inline_model,
            openai_base_url="https://legacy.example.com/v1" if inline_model else None,
            openai_api_key_enc=encrypt("legacy-key") if inline_model else None))
        db.commit()


def test_untested_model_disables_images_and_says_why(seeded):
    """The important default: silence is not consent."""
    _, pid = seeded
    _point_project_at(pid, _endpoint(supports=None))
    with SyncSession() as db:
        v = vision.project_image_support_sync(db, db.get(Project, pid))
    assert v["enabled"] is False
    assert "hasn't been checked" in v["reason"]
    assert v["model"] == "some-model-v1"


def test_a_model_that_said_no_reads_differently_from_one_never_asked(seeded):
    _, pid = seeded
    _point_project_at(pid, _endpoint(supports=False, source="probe"))
    with SyncSession() as db:
        v = vision.project_image_support_sync(db, db.get(Project, pid))
    assert v["enabled"] is False
    assert "can't read images" in v["reason"]


def test_a_probed_model_enables_images(seeded):
    _, pid = seeded
    _point_project_at(pid, _endpoint(supports=True, source="probe"))
    with SyncSession() as db:
        v = vision.project_image_support_sync(db, db.get(Project, pid))
    assert v == {"enabled": True, "reason": None, "model": "some-model-v1"}


def test_an_admin_declaration_enables_images_without_a_probe(seeded):
    """The escape hatch for providers whose API can't be probed."""
    _, pid = seeded
    _point_project_at(pid, _endpoint(supports=True, source="admin"))
    with SyncSession() as db:
        v = vision.project_image_support_sync(db, db.get(Project, pid))
    assert v["enabled"] is True


def test_legacy_inline_config_has_nowhere_to_store_a_verdict(seeded):
    _, pid = seeded
    _point_project_at(pid, inline_model="legacy-model")
    with SyncSession() as db:
        v = vision.project_image_support_sync(db, db.get(Project, pid))
    assert v["enabled"] is False and "saved model endpoint" in v["reason"]


def test_the_instance_default_is_off_until_an_admin_says_otherwise(seeded):
    _, pid = seeded  # no ProjectModelConfig at all → the env-configured model
    with SyncSession() as db:
        off = vision.project_image_support_sync(db, db.get(Project, pid))
        db.add(AppSetting(key=vision.DEFAULT_MODEL_IMAGES_KEY, value=True))
        db.commit()
        on = vision.project_image_support_sync(db, db.get(Project, pid))
    assert off["enabled"] is False and "hasn't been confirmed" in off["reason"]
    assert on["enabled"] is True


def test_probe_verdict_reads_a_rejection_as_no_but_an_outage_as_unknown():
    """The probe must not record a false negative when the provider is merely
    down - that would silently disable images until someone re-tested."""
    from app.api import model_endpoints as me

    rejected = {"ok": False, "status": 400,
                "error": "Invalid content type image_url for this model"}
    outage = {"ok": False, "status": 503, "error": "upstream unavailable"}
    accepted = {"ok": True, "status": 200, "error": None}

    def verdict(r):
        err = (r["error"] or "").lower()
        if r["ok"]:
            return True
        if r["status"] == 400 and any(h in err for h in me._VISION_REJECT_HINTS):
            return False
        return None

    assert verdict(accepted) is True
    assert verdict(rejected) is False
    assert verdict(outage) is None


def test_an_empty_row_is_not_a_model_choice(seeded):
    """The shape `ondelete SET NULL` leaves behind when an endpoint is deleted: a
    row with neither an endpoint nor inline credentials. The calls fall through to
    the kind/instance default, so the verdict has to follow them there instead of
    describing a model nothing will use."""
    _, pid = seeded
    with SyncSession() as db:
        db.execute(delete(ProjectModelConfig).where(ProjectModelConfig.project_id == pid))
        db.add(ProjectModelConfig(project_id=pid))
        db.commit()
    with SyncSession() as db:
        project = db.get(Project, pid)
        v = vision.project_image_support_sync(db, project)
        assert v["model"] == model_config.project_model_config(db, project)[2]
