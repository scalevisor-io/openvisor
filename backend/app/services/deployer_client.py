"""Client for the deployer service (DinD lifecycle + Traefik dynamic config).
Sync - used from Celery tasks only."""
import httpx

from app.core.config import settings


class DeployerError(Exception):
    pass


def _call(method: str, path: str, json_body: dict | None = None, timeout: float = 600) -> dict:
    try:
        r = httpx.request(method, f"{settings.deployer_url}{path}", json=json_body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        # Unwrap FastAPI's {"detail": ...} so chat/system messages show the
        # deployer's actual error text instead of escaped JSON.
        detail = exc.response.text
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:
            pass
        raise DeployerError(f"deployer {path}: {exc.response.status_code}\n{detail[:2000]}")
    except httpx.HTTPError as exc:
        raise DeployerError(f"deployer {path}: {exc}")


def start_demo(project_id: str, subdomain: str, port: int, htpasswd: str,
               workdir: str = ".") -> dict:
    return _call("POST", "/demos/start", {
        "project_id": project_id, "subdomain": subdomain, "port": port,
        "htpasswd": htpasswd, "workdir": workdir,
    })


def stop_dev_job(project_id: str, run_name: str = "") -> dict:
    """§14 stop: kill the project's in-flight dev runner (idempotent). The
    worker blocked in run_dev_job returns as the container/Job dies."""
    return _call("POST", "/dev/stop", {"project_id": project_id, "run_name": run_name}, timeout=180)


def verify_demo(project_id: str, workdir: str = ".", checks: list | None = None,
                name: str = "", run_dir: str = "", screenshots: list | None = None) -> dict:
    """§14.5 boot gate: test-boot the workspace's demo stack in a throwaway
    sandbox. {"ok": bool, "logs": str, "acceptance": {...}|None} - ok=false is
    agent-fixable (build/boot failure with diagnostics); infra errors raise
    DeployerError instead. `checks` (§Phase 1 #5) run against the booted app;
    the result rides back in "acceptance" (advisory, the caller never gates on it).
    Generous timeout: the sandbox may cold-pull images and build from scratch."""
    return _call("POST", "/demos/verify",
                 {"project_id": project_id, "workdir": workdir, "checks": checks or [],
                  "name": name, "run_dir": run_dir, "screenshots": screenshots or []},
                 timeout=1500)


def sbom_scan(project_id: str, workdir: str = ".") -> dict:
    """§Phase 2 DevSecOps gate: trivy SBOM + CVE scan of the built workspace.
    {scanned, components, findings, critical, high} - scanned=False on any deployer/
    trivy error, which the caller treats as fail-open (never blocks a build)."""
    return _call("POST", "/scan/sbom", {"project_id": project_id, "workdir": workdir},
                 timeout=700)


def stop_demo(project_id: str, subdomain: str) -> dict:
    return _call("POST", "/demos/stop", {"project_id": project_id, "subdomain": subdomain})


def redeploy_demo(project_id: str, subdomain: str, port: int, htpasswd: str,
                  workdir: str = ".") -> dict:
    return _call("POST", "/demos/redeploy", {
        "project_id": project_id, "subdomain": subdomain, "port": port,
        "htpasswd": htpasswd, "workdir": workdir,
    })


def run_program(name: str, run_dir: str, *, timeout_s: int, cpu_limit: str,
                mem_limit: str, cpu_request: str = "", mem_request: str = "",
                extra_host: str = "") -> dict:
    """§28 program run in a throwaway DinD. The worker has staged the sandbox
    content under <workspaces>/<run_dir>/work. Same margin rule as dev runs:
    the HTTP client must outlive the deployer's own watchdog (plus DinD boot
    and image pulls, hence the fatter margin). `extra_host` carries the same
    tailnet git-host alias dev-run sandboxes get - programs clone customer
    repositories too, and a tailnet-only forge is unreachable without it."""
    return _call("POST", "/programs/run", {
        "name": name, "run_dir": run_dir, "timeout_s": timeout_s,
        "cpu_limit": cpu_limit, "mem_limit": mem_limit,
        "cpu_request": cpu_request, "mem_request": mem_request,
        "extra_host": extra_host,
    }, timeout=timeout_s + 300)


def cleanup_program_sandbox(name: str) -> dict:
    """Remove a program sandbox and its layer-cache volume (instance deleted)."""
    return _call("POST", "/programs/cleanup", {"name": name}, timeout=120)


def run_dev_job(project_id: str, *, llm_model: str, llm_api_key: str, llm_base_url: str,
                agent_branch: str = "agent/mvp", git_push: bool = False,
                plan_only: bool = False, reasoning_effort: str | None = None,
                remote_url: str = "", default_branch: str = "main", extra_host: str = "",
                gitlab_host: str = "", provider: str = "gitlab", max_iterations: int = 0,
                skip_agent: bool = False, run_dir: str = "", run_name: str = "",
                git_author_name: str = "", git_author_email: str = "", brand_name: str = "",
                egress_locked: bool = False, egress_allowlist: list[str] | None = None,
                cpu_request: str = "", mem_request: str = "",
                harness: str = "openhands", max_usd: float = 0.0,
                timeout_s: int = 1800) -> dict:
    # Give the deployer its full wall-clock budget plus a margin so the HTTP
    # client never times out before the deployer's own watchdog fires (the old
    # fixed 600s cut off any build longer than 10 min and orphaned the runner).
    return _call("POST", "/dev/run", {
        "project_id": project_id, "llm_model": llm_model, "llm_api_key": llm_api_key,
        "llm_base_url": llm_base_url, "agent_branch": agent_branch,
        "plan_only": plan_only, "reasoning_effort": reasoning_effort,
        "git_push": git_push, "remote_url": remote_url, "default_branch": default_branch,
        "extra_host": extra_host, "gitlab_host": gitlab_host,
        "provider": provider, "max_iterations": max_iterations,
        "skip_agent": skip_agent, "run_dir": run_dir, "run_name": run_name,
        "git_author_name": git_author_name, "git_author_email": git_author_email,
        "brand_name": brand_name,
        "egress_locked": egress_locked, "egress_allowlist": egress_allowlist or [],
        "cpu_request": cpu_request, "mem_request": mem_request,
        # §dev harness: the driver the runner image executes for this dispatch.
        "harness": harness,
        # Per-run provider-side spend ceiling in USD (0 = unset); only a driver
        # whose SDK takes a budget can honor it.
        "max_usd": max_usd,
        "timeout_s": timeout_s,
    }, timeout=timeout_s + 120)
