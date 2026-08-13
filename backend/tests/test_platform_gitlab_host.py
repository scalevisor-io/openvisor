"""§ssh remotes: the platform recognises its OWN GitLab under BOTH hostnames.

An instance knows its forge by two names that need not match - the API host from
GITLAB_URL (what /api/v4 answers on) and the SSH host from GITLAB_SSH_HOST (what
git dials). A repo cloned over the SSH name was treated as a stranger's forge:
detected as `other` (so no issue polling, no MR integration), and when forced to
`gitlab` its API base was derived from the SSH host - a hostname that need not
serve /api/v4, and on a tailnet deployment is not routable from the api/worker
pods at all.

The security-relevant half is the token: the platform GitLab token must reach
the platform's own host and NOTHING else.
"""
import pytest

from app.core.config import settings
from app.models import Organization, Project, ProjectMemory
from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.services import gitlab, repos as repolib
from app.workers import tasks

PLATFORM_SSH = "ssh://git@git.acme.test:10022/acme.ai/carouter.git"
PLATFORM_SCP = "git@git.acme.test:acme.ai/carouter.git"
PLATFORM_API = "git@gitlab.acme.test:acme.ai/carouter.git"
CUSTOMER_SELFHOSTED = "git@gitlab.customer.example:them/theirs.git"
CUSTOMER_COM = "git@gitlab.com:them/theirs.git"


@pytest.fixture(autouse=True)
def _hosts(monkeypatch):
    monkeypatch.setattr(settings, "gitlab_url", "https://gitlab.acme.test")
    monkeypatch.setattr(settings, "gitlab_ssh_host", "git.acme.test")
    monkeypatch.setattr(settings, "gitlab_token", "platform-token-xyz")


# ---------------------------------------------------------------- recognition

def test_the_ssh_hostname_is_recognised_as_the_platform_gitlab():
    for uri in (PLATFORM_SSH, PLATFORM_SCP, PLATFORM_API):
        assert gitlab.is_platform_host(uri), uri
        assert gitlab.is_gitlab(uri), uri
        assert repolib.detect_provider(uri) == "gitlab", uri


def test_customer_gitlab_hosts_are_not_the_platform():
    for uri in (CUSTOMER_SELFHOSTED, CUSTOMER_COM):
        assert not gitlab.is_platform_host(uri), uri
        assert gitlab.is_gitlab(uri), uri  # still GitLab, just not ours


def test_an_unset_ssh_host_changes_nothing(monkeypatch):
    """The setting is optional: deployments whose two hostnames match must
    behave exactly as before."""
    monkeypatch.setattr(settings, "gitlab_ssh_host", "")
    assert not gitlab.is_platform_host(PLATFORM_SSH)
    assert repolib.detect_provider(PLATFORM_SSH) == "other"
    assert gitlab.is_platform_host(PLATFORM_API)  # the API host still is ours


# ---------------------------------------------------------------- API base

def test_platform_repos_resolve_to_the_configured_api_base():
    """Deriving https://<ssh-host> is what made the sweep unpollable: only the
    API hostname serves /api/v4."""
    for uri in (PLATFORM_SSH, PLATFORM_SCP, PLATFORM_API):
        assert gitlab.customer_base_url(uri) == "https://gitlab.acme.test", uri


def test_customer_repos_still_resolve_to_their_own_host():
    assert gitlab.customer_base_url(CUSTOMER_SELFHOSTED) == "https://gitlab.customer.example"
    assert gitlab.customer_base_url(CUSTOMER_COM) == "https://gitlab.com"


# ---------------------------------------------------------------- the token

def _project(db, **kw):
    org = Organization(name="Tok Org")
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai", **kw)
    db.add(p)
    db.flush()
    return p


def test_platform_token_falls_back_only_for_our_own_host():
    with SyncSession() as db:
        try:
            project = _project(db)
            assert tasks._project_repo_token(
                db, project, "gitlab", PLATFORM_SSH) == "platform-token-xyz"
            assert tasks._project_repo_token(
                db, project, "gitlab", PLATFORM_API) == "platform-token-xyz"
        finally:
            db.rollback()


def test_the_platform_token_never_travels_to_a_customer_host():
    """The whole reason the fallback is keyed on the repo URI."""
    with SyncSession() as db:
        try:
            project = _project(db)
            for uri in (CUSTOMER_SELFHOSTED, CUSTOMER_COM):
                assert tasks._project_repo_token(db, project, "gitlab", uri) is None, uri
            # and a caller that names no repo gets nothing either
            assert tasks._project_repo_token(db, project, "gitlab") is None
        finally:
            db.rollback()


def test_a_project_token_still_wins_over_the_platform_one():
    """A customer PAT on the project keeps precedence - the fallback is only
    for when they have not set one."""
    with SyncSession() as db:
        try:
            project = _project(db)
            db.add(ProjectMemory(project_id=project.id, key="GITLAB_TOKEN",
                                 value_enc=encrypt("customer-pat"), is_secret=True,
                                 author="customer"))
            db.flush()
            assert tasks._project_repo_token(
                db, project, "gitlab", PLATFORM_SSH) == "customer-pat"
        finally:
            db.rollback()
