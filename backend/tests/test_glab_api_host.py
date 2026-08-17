"""§glab api host: the sandbox's `glab` talks to the host /api/v4 answers on.

An instance may serve git over one hostname and its API over another
(`GITLAB_SSH_HOST` vs `GITLAB_URL`). The runner used to derive glab's host from
the push remote, i.e. the SSH name - so on such an instance every `glab` call
left the sandbox for a host that does not serve the API and landed on whatever
else answers there. Observed live as a 502 carrying an unrelated site's TLS
certificate, while the agent was trying to file the issue its task asked for.

`_gitlab_api_host` resolves it with `gitlab.customer_base_url` - the same
resolver the worker's own API calls use - so the two can no longer disagree.
"""
import pytest

from app.core.config import settings
from app.workers.tasks import _gitlab_api_host


@pytest.fixture
def split_hostnames(monkeypatch):
    """An instance whose SSH and API hostnames differ - the case that broke."""
    monkeypatch.setattr(settings, "gitlab_url", "https://gitlab.example.com")
    monkeypatch.setattr(settings, "gitlab_ssh_host", "git.example.com")


def test_the_ssh_hostname_resolves_to_the_api_hostname(split_hostnames):
    target = {"provider": "gitlab_customer",
              "remote": "ssh://git@git.example.com:10022/team/app.git"}
    assert _gitlab_api_host(target) == "https://gitlab.example.com"


def test_the_platform_repo_resolves_to_gitlab_url(split_hostnames):
    target = {"provider": "gitlab", "remote": "ssh://git@git.example.com:10022/g/p.git"}
    assert _gitlab_api_host(target) == "https://gitlab.example.com"


def test_a_third_party_gitlab_keeps_its_own_host(split_hostnames):
    target = {"provider": "gitlab_customer", "remote": "git@gitlab.com:acme/app.git"}
    assert _gitlab_api_host(target) == "https://gitlab.com"


def test_runner_provider_wins_over_provider(split_hostnames):
    """_dev_target sets runner_provider for the push mode; it is the truthier one."""
    target = {"provider": "other", "runner_provider": "gitlab_customer",
              "remote": "ssh://git@git.example.com:10022/team/app.git"}
    assert _gitlab_api_host(target) == "https://gitlab.example.com"


def test_non_gitlab_providers_get_nothing():
    assert _gitlab_api_host({"provider": "github",
                             "remote": "git@github.com:acme/app.git"}) == ""
    assert _gitlab_api_host({"provider": "other", "remote": "git@git.acme.dev:a/b.git"}) == ""


def test_an_unrecognisable_remote_falls_back_to_the_runner(split_hostnames):
    """Empty means 'derive it yourself' - never a failed dispatch over a hostname."""
    assert _gitlab_api_host({"provider": "gitlab_customer", "remote": ""}) == ""
