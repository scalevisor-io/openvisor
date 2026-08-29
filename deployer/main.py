"""Openvisor deployer - owns the per-project DinD lifecycle and the Traefik
dynamic config (PROMPT §17). Runs with the host docker socket; demo containers
are hardened siblings on the isolated `openvisor-demos` network:
  - local:  --privileged docker:dind (no sysbox on dev machines)
  - prod:   sysbox-runc runtime (rootless DinD), if DEMO_RUNTIME=sysbox-runc
Both get dropped capabilities where possible, CPU/mem/pids limits, no platform
bind-mounts (the workspace is docker-cp'd in), and no access to the platform's
internal network."""
import json
import logging
import os
import re
import select
import subprocess
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import browsershot
import healthcheck
import programs_common

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deployer")

app = FastAPI(title="Openvisor deployer")

DEPLOY_ENV = os.environ["DEPLOY_ENV"]
DEPLOY_DOMAIN = os.environ["DEPLOY_DOMAIN"]
# docker (compose deployment, this file's subprocess implementation) or
# kubernetes (Helm deployment: demos as DinD pods, dev runs as Jobs - see k8s.py)
ORCHESTRATOR = os.environ.get("ORCHESTRATOR", "docker")
CPU_LIMIT = os.environ.get("DEMO_CPU_LIMIT", "1")
MEM_LIMIT = os.environ.get("DEMO_MEM_LIMIT", "2g")
RUNNER_CPU_LIMIT = os.environ.get("RUNNER_CPU_LIMIT", CPU_LIMIT)
RUNNER_MEM_LIMIT = os.environ.get("RUNNER_MEM_LIMIT", MEM_LIMIT)

if ORCHESTRATOR == "kubernetes":
    import k8s as k8s_backend
RUNTIME = os.environ.get("DEMO_RUNTIME", "")  # e.g. sysbox-runc in prod
# §dev-docker: give dev-run sandboxes an INNER docker daemon (the runner
# entrypoint starts dockerd when DEV_DOCKER=1). Needs a runtime that allows it:
# RUNTIME (Sysbox) in production, --privileged as the local-dev fallback -
# the same posture demo DinD containers already run with.
DEV_SANDBOX_DOCKER = os.environ.get("DEV_SANDBOX_DOCKER", "0") == "1"
DYNAMIC_DIR = "/traefik-dynamic"
DEV_MAX_CONCURRENT_RUNS = int(os.environ.get("DEV_MAX_CONCURRENT_RUNS", "0"))  # 0 = no global backstop
WORKSPACES = "/workspaces"
WORKSPACES_VOLUME = os.environ.get("WORKSPACES_VOLUME", "openvisor_workspaces")
DEMOS_NETWORK = os.environ.get("DEMOS_NETWORK", "openvisor-demos")
DIND_IMAGE = "docker:27-dind"
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "openvisor-runner")
PLATFORM_NETWORK = os.environ.get("PLATFORM_NETWORK", "openvisor_internal")


class DemoIn(BaseModel):
    project_id: str
    subdomain: str
    port: int = 0
    htpasswd: str = ""
    workdir: str = "."  # subdir of the workspace holding compose.demo.yml


class SbomIn(BaseModel):
    project_id: str
    workdir: str = "."


class VerifyIn(BaseModel):
    project_id: str
    port: int = 18080  # any free port inside the throwaway DinD works
    workdir: str = "."
    # §Phase 1 #5: spec-derived acceptance checks to run once the demo boots.
    # Each: {path, contains:[...], desc}. Advisory - the worker never gates on them.
    checks: list[dict] = []
    # §parallel-builds MR3 ('' = legacy verify-<project_id> + project workspace)
    name: str = ""
    run_dir: str = ""
    # §After-shots: [[width, height], ...] viewports to photograph once the app
    # answers (browsershot.py via browser-mcp, inside the verify window - the
    # sandbox dies in the finally). [] = no screenshots. Always best-effort.
    screenshots: list = []


class DevStopIn(BaseModel):
    project_id: str
    run_name: str = ""  # §parallel-builds MR2: '' = legacy dev-<project_id>


class DevRunIn(BaseModel):
    project_id: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    agent_branch: str = "agent/mvp"
    git_push: bool = False
    remote_url: str = ""
    default_branch: str = "main"
    extra_host: str = ""  # "host:ip" for GitLab resolution
    # §glab api host: the base URL /api/v4 answers on, which need NOT be the host
    # git dials (an instance can serve SSH on git.example.com and the API on
    # gitlab.example.com). '' lets the runner fall back to deriving it from the
    # remote, which is only correct when the two names match.
    gitlab_host: str = ""
    # §git identity: git user.name / user.email for the agent's commits; '' lets
    # the runner fall back to its own default.
    git_author_name: str = ""
    git_author_email: str = ""
    # The brand the wrapper commit is titled with ("<brand> agent: MVP build"), so a
    # white-label instance's commits never carry the upstream name into customer repos.
    brand_name: str = "Openvisor"
    provider: str = "gitlab"  # gitlab | github (changes the runner's push mode)
    max_iterations: int = 0  # 0 = uncapped; bounds the agent's token spend
    skip_agent: bool = False  # push the pre-populated workspace as-is (no LLM run)
    plan_only: bool = False  # explore + write .openvisor/plan.md, no edits and no publish
    reasoning_effort: str | None = None  # §effort - LLM_REASONING_EFFORT for the driver
    # §parallel-builds MR2 (both '' = legacy single-run naming/paths):
    run_dir: str = ""   # workspace subdir relative to /workspaces; '' = <project_id>
    run_name: str = ""  # container/Job name; '' = dev-<project_id>
    # §egress: dev-sandbox egress lockdown. Enforced on Kubernetes only (a per-run
    # filtering proxy + NetworkPolicy); the compose path ignores both fields.
    egress_locked: bool = False
    egress_allowlist: list[str] = []
    # §dev-pod resources: per-project scheduling requests (docker-style, '' = the
    # instance defaults). K8s uses them as the pod's requests (raising the limit
    # when a request exceeds it); compose maps mem_request to --memory-reservation
    # and cannot honor a cpu request.
    cpu_request: str = ""
    mem_request: str = ""
    timeout_s: int = 1800


class ProgramRunIn(BaseModel):
    """One program run (§28): `docker compose build && docker compose run
    program` in a throwaway DinD. Resources are PER PROGRAM (admin-set), never
    the DEMO_* limits; the worker staged the sandbox content under
    /workspaces/<run_dir>/work before calling."""
    name: str  # prog-<instance-uuid> | progchk-<program-uuid>
    run_dir: str  # relative to /workspaces: programs/<instance>/runs/<run-uuid>
    timeout_s: int = 900
    cpu_limit: str = "1"
    mem_limit: str = "1g"
    mem_request: str = ""  # docker --memory-reservation (soft floor); "" = none
    cpu_request: str = ""  # honored on Kubernetes only (docker has no cpu request)
    # "host:target" - the SAME tailnet mapping dev-run sandboxes get (§ssh
    # remotes). A git host reachable only over the tailnet resolves to a CGNAT
    # address a sandbox cannot route to, so a program cloning the customer's
    # repositories hangs until git gives up with "Could not read from remote
    # repository". The inner program container runs network_mode: host, so the
    # DinD's /etc/hosts is the program's too.
    extra_host: str = ""


class ProgramCleanupIn(BaseModel):
    name: str  # sandbox to remove together with its layer-cache volume


NOISE_MARKERS = ("Pulling ", "Pull complete", "Extracting", "Downloading",
                 "Download complete", "Waiting", "Verifying Checksum", "Already exists")


def error_tail(output: str, limit: int = 800) -> str:
    """Tail of docker/compose output with image-pull progress noise dropped, so
    the actual error survives the size cap (docker redraws progress with \r)."""
    lines = []
    for raw in output.replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line or any(m in line for m in NOISE_MARKERS):
            continue
        lines.append(line)
    return "\n".join(lines)[-limit:]


def run(cmd: list[str], check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise HTTPException(500, f"{' '.join(cmd[:4])}… failed:\n{error_tail(proc.stderr)}")
    return proc


def container_name(project_id: str) -> str:
    return f"demo-{project_id}"


def container_state(name: str) -> str:
    proc = run(["docker", "inspect", "-f", "{{.State.Status}}", name], check=False)
    return proc.stdout.strip() if proc.returncode == 0 else "absent"


def wait_for_inner_docker(name: str, tries: int = 60) -> None:
    for _ in range(tries):
        if run(["docker", "exec", name, "docker", "info"], check=False, timeout=20).returncode == 0:
            return
        time.sleep(2)
    raise HTTPException(500, f"inner docker in {name} never became ready")


def ensure_compose_plugin(name: str) -> None:
    if run(["docker", "exec", name, "docker", "compose", "version"], check=False).returncode != 0:
        run(["docker", "exec", name, "apk", "add", "--no-cache", "docker-cli-compose"],
            check=False, timeout=180)


def create_dind(name: str, cpus: str = "", memory: str = "",
                memory_reservation: str = "", extra_host: str = "") -> None:
    """Defaults keep the demo/verify limits (DEMO_*); program sandboxes pass
    their own per-program resources. `extra_host` ("host:target") adds the
    tailnet git-host alias - inner containers on network_mode: host share this
    /etc/hosts, which is how a program reaches a tailnet-only forge."""
    cmd = [
        "docker", "run", "-d", "--name", name,
        "--network", DEMOS_NETWORK,
        f"--cpus={cpus or CPU_LIMIT}", f"--memory={memory or MEM_LIMIT}",
        "--pids-limit=1024",
        "--security-opt", "no-new-privileges=false",
        "-e", "DOCKER_TLS_CERTDIR=",
        "-v", f"{name}-data:/var/lib/docker",
        "--restart", "unless-stopped",
    ]
    if extra_host:
        cmd += ["--add-host", extra_host]
    if memory_reservation:
        cmd += [f"--memory-reservation={memory_reservation}"]
    if RUNTIME:
        cmd += ["--runtime", RUNTIME]
    else:
        cmd += ["--privileged"]
    cmd += [DIND_IMAGE]
    run(cmd)


def sync_workspace(name: str, project_id: str, src_rel: str = "") -> None:
    ws = os.path.join(WORKSPACES, src_rel or project_id)
    if not os.path.isdir(ws):
        raise HTTPException(400, f"workspace {ws} not found")
    run(["docker", "exec", name, "rm", "-rf", "/project"], check=False)
    run(["docker", "exec", name, "mkdir", "-p", "/project"])
    run(["docker", "cp", f"{ws}/.", f"{name}:/project"])


def compose_up(name: str, port: int, workdir: str = ".") -> None:
    ensure_compose_plugin(name)
    # base compose is optional (the agent may ship only compose.demo.yml)
    proj = "/project" if workdir in (".", "") else f"/project/{workdir}"
    script = (
        # The DinD hosts exactly one stack, but a redeploy can change the inner
        # compose PROJECT NAME (it derives from the workdir - e.g. the agent
        # relocating files to the repo root per dev-prompt rule 2): `up
        # --force-recreate` under the new name can't see the old stack, which
        # keeps $PORT bound → "driver failed programming external connectivity".
        # Clean slate the inner CONTAINERS first; images and named volumes stay,
        # so build cache / fast resume are unaffected.
        'old=$(docker ps -aq); [ -n "$old" ] && docker rm -f $old; '
        f"cd {proj} && "
        "files='-f compose.demo.yml'; "
        "[ -f compose.base.yml ] && files='-f compose.base.yml -f compose.demo.yml'; "
        # force-recreate so bind mounts re-resolve after the workspace is re-synced
        # on restart (rm -rf + docker cp changes the inode the old container held)
        "docker compose $files up -d --build --force-recreate"
    )
    run(["docker", "exec", "-e", f"PORT={port}", name, "sh", "-c", script], timeout=900)


def run_acceptance(name: str, port: int, checks: list) -> dict:
    """§Phase 1 #5: fetch each check's path from the just-booted app inside the
    DinD and match its `contains` fragments IN PYTHON (never a shell) - so an
    LLM-authored path or fragment can't inject a command into the sandbox. The URL
    is passed as a positional arg ($1), never interpolated into the script. Best
    effort: a fetch error means that one check fails, never an exception."""
    results = []
    for chk in (checks or [])[:5]:
        path = str((chk or {}).get("path") or "/")
        url = f"http://127.0.0.1:{int(port)}{path}"
        body = ""
        try:
            # Cap the body (head -c) so a hostile demo streaming a huge response
            # can't OOM the deployer - the boot probe discards the body entirely;
            # `contains` matching only needs the first chunk. "$1" stays quoted.
            proc = run(["docker", "exec", name, "sh", "-c",
                        'wget -q -O - -T 8 "$1" 2>/dev/null | head -c 65536', "_", url],
                       check=False, timeout=25)
            body = proc.stdout or ""
        except Exception:  # noqa: BLE001
            body = ""
        frags = [str(f) for f in ((chk or {}).get("contains") or [])]
        ok = bool(frags) and all(f in body for f in frags)
        results.append({"path": path, "desc": str((chk or {}).get("desc") or "")[:120],
                        "ok": ok})
    return {"passed": sum(1 for r in results if r["ok"]), "total": len(results),
            "results": results}


def wait_for_app(name: str, port: int, workdir: str) -> None:
    """Readiness gate (PROMPT §17): compose up succeeds even when the app
    container crash-loops, so require an HTTP answer on the published port
    before routing the demo - otherwise fail the start with the inner compose
    state + log tail instead of leaving a URL that 502s."""
    if healthcheck.TIMEOUT_S <= 0:
        return
    proc = run(["docker", "exec", name, "sh", "-c",
                healthcheck.probe_script(port, healthcheck.TIMEOUT_S)],
               check=False, timeout=healthcheck.TIMEOUT_S + 60)
    if healthcheck.READY in proc.stdout:
        return
    diag = run(["docker", "exec", name, "sh", "-c",
                healthcheck.diagnostics_script(port, workdir)],
               check=False, timeout=120)
    raise HTTPException(500, (
        f"demo app did not answer HTTP on port {port} within "
        f"{healthcheck.TIMEOUT_S}s after compose up:\n{diag.stdout[-2400:]}"))


def write_traefik_config(project_id: str, subdomain: str, port: int, htpasswd: str) -> None:
    name = container_name(project_id)
    host = f"{subdomain}.{DEPLOY_DOMAIN}"
    tls = "" if DEPLOY_ENV == "local" else "      tls: {}\n"
    config = (
        "http:\n"
        "  routers:\n"
        f"    {name}:\n"
        f"      rule: Host(`{host}`)\n"
        f"      service: {name}\n"
        f"      middlewares: [\"{name}-auth\"]\n"
        + tls +
        "  middlewares:\n"
        f"    {name}-auth:\n"
        "      basicAuth:\n"
        "        users:\n"
        f"          - \"{htpasswd}\"\n"
        "  services:\n"
        f"    {name}:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        f"          - url: \"http://{name}:{port}\"\n"
    )
    with open(os.path.join(DYNAMIC_DIR, f"{name}.yml"), "w") as f:
        f.write(config)


def remove_traefik_config(project_id: str) -> None:
    path = os.path.join(DYNAMIC_DIR, f"{container_name(project_id)}.yml")
    if os.path.exists(path):
        os.remove(path)


@app.post("/demos/start")
def start(body: DemoIn):
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.demo_start(body.project_id, body.subdomain, body.port,
                                      body.htpasswd, body.workdir,
                                      CPU_LIMIT, MEM_LIMIT, DEPLOY_DOMAIN)
    name = container_name(body.project_id)
    state = container_state(name)
    if state == "absent":
        create_dind(name)
    elif state != "running":
        run(["docker", "start", name])
    wait_for_inner_docker(name)
    sync_workspace(name, body.project_id)
    compose_up(name, body.port, body.workdir)
    wait_for_app(name, body.port, body.workdir)
    write_traefik_config(body.project_id, body.subdomain, body.port, body.htpasswd)
    return {"ok": True, "container": name, "state": "running"}


@app.post("/demos/redeploy")
def redeploy(body: DemoIn):
    return start(body)


@app.post("/demos/stop")
def stop(body: DemoIn):
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.demo_stop(body.project_id)
    name = container_name(body.project_id)
    remove_traefik_config(body.project_id)
    if container_state(name) == "running":
        # stop the whole DinD, named volumes preserved (§17 fast resume)
        run(["docker", "stop", "-t", "20", name])
    return {"ok": True, "container": name, "state": "stopped"}


def verify_name(project_id: str) -> str:
    return f"verify-{project_id}"


@app.post("/demos/verify")
def verify(body: VerifyIn):
    """§14.5 boot gate: boot the workspace's demo stack in a THROWAWAY DinD -
    never the live demo, so a request-scoped run can't clobber a running demo -
    and report whether the app answers HTTP. Always 200 on an app-side failure
    (ok=false + logs: agent-fixable, the worker feeds it back to the runner);
    only infra errors 5xx. The verify container is removed afterwards; its
    named volume is kept so a fix attempt's rebuild hits the docker cache."""
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.demo_verify(body.project_id, body.port, body.workdir,
                                       CPU_LIMIT, MEM_LIMIT,
                                       name=body.name, run_dir=body.run_dir,
                                       screenshots=body.screenshots)
    name = body.name or verify_name(body.project_id)
    if body.name and not re.match(r"^verify-[0-9a-fA-F-]{1,64}(-[0-9a-fA-F]{1,16})?$", body.name):
        raise HTTPException(400, "bad verify name")
    state = container_state(name)
    if state == "absent":
        create_dind(name)
    elif state != "running":
        run(["docker", "start", name])
    try:
        wait_for_inner_docker(name)
        sync_workspace(name, body.project_id, body.run_dir)
        try:
            compose_up(name, body.port, body.workdir)
            wait_for_app(name, body.port, body.workdir)
        except HTTPException as exc:
            return {"ok": False, "logs": str(exc.detail)}
        # Booted - run the advisory acceptance checks against it (§Phase 1 #5).
        acceptance = run_acceptance(name, body.port, body.checks) if body.checks else None
        # §After-shots: the verify DinD publishes $PORT on its own interface and
        # browser-mcp shares the demos network, so the sandbox is photographable
        # by name for exactly as long as this window stays open.
        shots = (browsershot.capture(f"http://{name}:{body.port}", body.screenshots)
                 if body.screenshots else [])
        return {"ok": True, "logs": "", "acceptance": acceptance, "screenshots": shots}
    finally:
        run(["docker", "rm", "-f", name], check=False)


TRIVY_IMAGE = os.environ.get("TRIVY_IMAGE", "aquasec/trivy:latest")
TRIVY_CACHE_VOLUME = os.environ.get("TRIVY_CACHE_VOLUME", "openvisor_trivy-cache")
# A single safe path segment (project id / workdir) - no slashes, no `..`, no shell/`-v`
# metacharacters - so a request field can't become a traversal or a malformed bind mount.
_SAFE_SEG = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@app.post("/scan/sbom")
def scan_sbom(body: SbomIn):
    """§Phase 2 DevSecOps gate: generate an SBOM (component inventory) for the built
    workspace and scan it for CVEs with trivy (filesystem scan of the dependency
    manifests). Returns {scanned, components:[{name,version}], findings:[CRITICAL/HIGH
    CVEs], critical, high}. `scanned=False` (with `error`) on any trivy/infra problem -
    the caller treats that as fail-open (defence-in-depth, not the sole control), and
    never blocks a build on the deployer's tooling."""
    if ORCHESTRATOR == "kubernetes":
        # No host docker daemon to run trivy; the k8s trivy-as-Job backend is a
        # follow-up. Explicit (logged) unavailability, not a silent docker-run failure.
        log.warning("sbom gate unavailable on kubernetes orchestrator; scan skipped")
        return {"scanned": False, "error": "sbom scan unavailable on kubernetes"}
    # Validate inputs before they become a host bind mount (defense-in-depth, mirroring
    # the program sandbox): project_id/workdir must be safe path segments, and the
    # resolved scan path must stay inside the workspaces mount - no traversal.
    if not _SAFE_SEG.match(body.project_id) or (
            body.workdir not in (".", "") and not _SAFE_SEG.match(body.workdir)):
        return {"scanned": False, "error": "invalid path segment"}
    if not os.path.isdir(os.path.join(WORKSPACES, body.project_id)):
        return {"scanned": False, "error": "workspace not present"}
    base = os.path.realpath(WORKSPACES_VOLUME_MOUNT())
    ws_host = os.path.join(base, body.project_id)
    scan_path = ws_host if body.workdir in (".", "") else os.path.join(ws_host, body.workdir)
    real = os.path.realpath(scan_path)
    if real != base and not real.startswith(base + os.sep):
        return {"scanned": False, "error": "path escapes workspace"}
    try:
        proc = run(["docker", "run", "--rm",
                    "-v", f"{scan_path}:/scan:ro",
                    "-v", f"{TRIVY_CACHE_VOLUME}:/root/.cache/trivy",
                    TRIVY_IMAGE, "fs", "--scanners", "vuln",
                    "--severity", "CRITICAL,HIGH", "--format", "json",
                    "--quiet", "--list-all-pkgs", "/scan"],
                   check=False, timeout=600)
        if proc.returncode != 0:
            return {"scanned": False, "error": error_tail(proc.stderr)}
        data = json.loads(proc.stdout or "{}")
    except Exception as exc:  # noqa: BLE001 - never fail a build on the scanner
        log.warning("sbom scan error for %s: %s", body.project_id, exc)
        return {"scanned": False, "error": str(exc)[:400]}

    components: list[dict] = []
    findings: list[dict] = []
    for res in data.get("Results") or []:
        for pkg in res.get("Packages") or []:
            components.append({"name": pkg.get("Name"), "version": pkg.get("Version")})
        for v in res.get("Vulnerabilities") or []:
            findings.append({"id": v.get("VulnerabilityID"), "severity": v.get("Severity"),
                             "pkg": v.get("PkgName"), "version": v.get("InstalledVersion"),
                             "fixed": v.get("FixedVersion")})
    return {
        "scanned": True,
        "components": components[:500],
        "component_count": len(components),
        "findings": findings[:100],
        "critical": sum(1 for f in findings if f.get("severity") == "CRITICAL"),
        "high": sum(1 for f in findings if f.get("severity") == "HIGH"),
    }


def runner_name(project_id: str) -> str:
    return f"dev-{project_id}"


_DEV_RUN_DIR_RE = re.compile(r"^devruns/[0-9a-fA-F-]{1,64}/[0-9a-fA-F-]{1,64}$")
_DEV_RUN_NAME_RE = re.compile(r"^dev-[0-9a-fA-F-]{1,64}(-[0-9a-fA-F]{1,16})?$")


def _dev_paths(body_project_id: str, run_dir: str, run_name: str) -> tuple[str, str]:
    """Resolve (workspace-relative dir, container name) for a dev run, with the
    Programs-style validation discipline on the per-run fields ('' = legacy).
    Realpath containment guards a crafted run_dir escaping /workspaces."""
    if run_name and not _DEV_RUN_NAME_RE.match(run_name):
        raise HTTPException(400, "bad run_name")
    if run_dir:
        if not _DEV_RUN_DIR_RE.match(run_dir):
            raise HTTPException(400, "bad run_dir")
        resolved = os.path.realpath(os.path.join(WORKSPACES, run_dir))
        if not resolved.startswith(os.path.realpath(WORKSPACES) + os.sep):
            raise HTTPException(400, "bad run_dir")
    return run_dir or body_project_id, run_name or runner_name(body_project_id)


@app.post("/dev/run")
def dev_run(body: DevRunIn):
    """Launch a sandboxed OpenHands dev job for one project (§14). The worker has
    already written /workspaces/<id>/.openvisor/{task.md,mcp.json,deploy_key}. We
    bind-mount that project subdir as the runner's /workspace and run headless.
    Returns when the runner exits (bounded by timeout_s).

    §egress: `body.egress_locked`/`egress_allowlist` are enforced only on the
    Kubernetes path (a per-run filtering proxy + NetworkPolicy). The compose path
    below deliberately IGNORES them - like every sandbox network control in this
    repo, egress lockdown is a K8s-only boundary; compose dev-runs keep their
    existing open egress on PLATFORM_NETWORK."""
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.dev_run(body, RUNNER_CPU_LIMIT, RUNNER_MEM_LIMIT)
    if DEV_MAX_CONCURRENT_RUNS > 0:
        live = run(["docker", "ps", "--filter", "name=dev-", "--format", "{{.Names}}"],
                   check=False).stdout.strip().splitlines()
        if len([n for n in live if n.startswith("dev-")]) >= DEV_MAX_CONCURRENT_RUNS:
            raise HTTPException(429, "dev-run capacity reached - retry shortly")
    rel, name = _dev_paths(body.project_id, body.run_dir, body.run_name)
    ws_host = os.path.join(WORKSPACES_VOLUME_MOUNT(), rel)
    if not os.path.isdir(os.path.join(WORKSPACES, rel)):
        raise HTTPException(400, f"workspace for {rel} not found")

    run(["docker", "rm", "-f", name], check=False)
    cmd = [
        "docker", "run", "-d", "--name", name,
        "--network", PLATFORM_NETWORK,  # reach context7/browser-mcp in-network (browser-mcp's page loads reach the agent's dev server back)
        f"--cpus={CPU_LIMIT}", f"--memory={MEM_LIMIT}", "--pids-limit=2048",
    ]
    if body.mem_request:
        # §dev-pod resources: docker's closest analogue of a memory request.
        cmd += [f"--memory-reservation={body.mem_request}"]
    cmd += [
        "-v", f"{ws_host}:/workspace",
        "-e", f"LLM_MODEL={body.llm_model}",
        "-e", f"LLM_API_KEY={body.llm_api_key}",
        "-e", f"LLM_BASE_URL={body.llm_base_url}",
        "-e", f"AGENT_BRANCH={body.agent_branch}",
        "-e", f"GIT_PUSH={'1' if body.git_push else '0'}",
        "-e", f"GIT_REMOTE_URL={body.remote_url}",
        "-e", f"GIT_DEFAULT_BRANCH={body.default_branch}",
        "-e", f"GIT_USER_NAME={body.git_author_name}",
        "-e", f"GIT_USER_EMAIL={body.git_author_email}",
        "-e", f"BRAND_NAME={body.brand_name}",
        "-e", f"GIT_PROVIDER={body.provider}",
        # §glab api host: the API base, which need not be the host git dials.
        "-e", f"GITLAB_HOST={body.gitlab_host}",
        "-e", f"LLM_MAX_ITERATIONS={body.max_iterations}",
        "-e", f"SKIP_AGENT={'1' if body.skip_agent else '0'}",
        "-e", f"PLAN_ONLY={'1' if body.plan_only else '0'}",
        "-e", f"LLM_REASONING_EFFORT={body.reasoning_effort or ''}",
        # Stable per-workspace id (resume chains share it): the runner sends it as
        # prompt_cache_key so providers with opt-in prompt caching (Mistral) serve
        # + report cache reads, billed at the §18 cached rate. Last path segment
        # only (the run id): OpenAI caps prompt_cache_key at 64 chars and the
        # full devruns/<pid>/<rid> path is 85 - it 400s EVERY call of the build.
        "-e", f"LLM_CACHE_KEY=dev-{rel.rsplit('/', 1)[-1]}",
    ]
    if DEV_SANDBOX_DOCKER:
        cmd += (["--runtime", RUNTIME] if RUNTIME else ["--privileged"])
        cmd += ["-e", "DEV_DOCKER=1"]
    if body.extra_host:
        cmd += ["--add-host", body.extra_host]
    cmd.append(RUNNER_IMAGE)
    run(cmd)
    # Wait for completion (bounded); the runner writes results into .openvisor/.
    deadline = time.time() + body.timeout_s
    timed_out = True
    while time.time() < deadline:
        state = container_state(name)
        if state in ("exited", "dead", "absent"):
            timed_out = False
            break
        time.sleep(5)
    # A container still running at the deadline is a TIMEOUT, not a success:
    # `docker inspect ExitCode` reports 0 while it runs, so read the real state
    # first and only trust the exit code once the container has actually stopped.
    logs = run(["docker", "logs", "--tail", "60", name], check=False)
    # docker logs splits the container's streams: stderr carries the actual
    # git/bash errors when the entrypoint dies, so ship both to the worker.
    log_text = logs.stdout + (("\n--- stderr ---\n" + logs.stderr) if logs.stderr else "")
    if timed_out:
        run(["docker", "rm", "-f", name], check=False)
        return {"ok": False, "exit_code": "timeout", "timed_out": True,
                "logs": log_text[-4000:]}
    code = run(["docker", "inspect", "-f", "{{.State.ExitCode}}", name], check=False)
    exit_code = code.stdout.strip() if code.returncode == 0 else "unknown"
    run(["docker", "rm", "-f", name], check=False)
    return {"ok": exit_code == "0", "exit_code": exit_code, "timed_out": False,
            "logs": log_text[-4000:]}


@app.post("/dev/stop")
def dev_stop(body: DevStopIn):
    """Kill an in-flight dev runner (§14 stop). Idempotent; the blocked /dev/run
    poll sees the container/Job gone and returns to the worker, which parks the
    run as stopped-and-resumable."""
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.dev_stop(body.project_id, body.run_name)
    _, name = _dev_paths(body.project_id, "", body.run_name)
    was_running = container_state(name) == "running"
    run(["docker", "rm", "-f", name], check=False)
    return {"ok": True, "was_running": was_running}


def WORKSPACES_VOLUME_MOUNT() -> str:
    """Host path backing the workspaces named volume (so sibling runner containers
    can bind-mount a project subdir)."""
    proc = run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", WORKSPACES_VOLUME],
               check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return WORKSPACES  # fallback (only valid if deployer shares the same mount)


# ---------------------------------------------------------------- programs (§28)

_PROG_NAME_RE = re.compile(r"^prog(chk)?-[0-9a-fA-F-]{1,64}$")
_PROG_RUN_DIR_RE = re.compile(r"^programs/(check-)?[0-9a-fA-F-]{1,64}/runs/[0-9a-fA-F-]{1,64}$")


def _program_paths(run_dir: str) -> tuple[str, str, str]:
    """(abs run dir, abs work dir, abs log path), traversal-guarded."""
    if not _PROG_RUN_DIR_RE.match(run_dir):
        raise HTTPException(400, f"invalid program run_dir '{run_dir}'")
    root = os.path.realpath(WORKSPACES)
    run_abs = os.path.realpath(os.path.join(WORKSPACES, run_dir))
    if not run_abs.startswith(root + os.sep):
        raise HTTPException(400, "run_dir escapes the workspaces volume")
    return run_abs, os.path.join(run_abs, "work"), os.path.join(run_abs, "run.log")


def stream_exec(name: str, script: str, log_path: str, deadline: float) -> int | None:
    """Run a shell script inside the sandbox, appending its combined output to
    log_path AS IT ARRIVES (the API serves that file live to the SPA). Returns
    the exit code, or None once the wall-clock deadline passes - the caller
    tears the whole sandbox down, which also kills the inner containers a dead
    exec would otherwise leave running."""
    log.info("$ docker exec %s sh -c <%s phase>", name, script.split()[0])
    proc = subprocess.Popen(["docker", "exec", name, "sh", "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    fd = proc.stdout.fileno()
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=10)
                return None
            ready, _, _ = select.select([fd], [], [], min(5.0, remaining))
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                lf.write(chunk.decode("utf-8", "replace"))
                lf.flush()
            elif proc.poll() is not None:
                break
    return proc.wait()


def extract_out(name: str, inner: str, dest: str) -> None:
    """Copy a path out of the sandbox (best-effort: absent paths are fine -
    e.g. a program that wrote no usage.json)."""
    run(["docker", "cp", f"{name}:{inner}", dest], check=False)


@app.post("/programs/run")
def program_run(body: ProgramRunIn):
    """§28: run one program job in a THROWAWAY DinD - `docker compose build`,
    a deploy validation, then `docker compose run --rm program` - streaming the
    log to <run_dir>/run.log and copying output/ + usage.json out afterwards.
    The container is always removed; its {name}-data volume is kept so the next
    run of the same instance hits the docker layer cache. Always 200 with the
    phase verdicts (program failures are data, not infra errors); only
    sandbox-level problems 5xx."""
    if not _PROG_NAME_RE.match(body.name):
        raise HTTPException(400, f"invalid program sandbox name '{body.name}'")
    run_abs, work, log_path = _program_paths(body.run_dir)
    if not os.path.isdir(work):
        raise HTTPException(400, f"run workspace {body.run_dir}/work not found")
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.program_run(body, run_abs, work, log_path)
    name = body.name

    run(["docker", "rm", "-f", name], check=False)
    create_dind(name, cpus=body.cpu_limit, memory=body.mem_limit,
                memory_reservation=body.mem_request, extra_host=body.extra_host)
    try:
        wait_for_inner_docker(name)
        ensure_compose_plugin(name)
        run(["docker", "exec", name, "rm", "-rf", "/project"], check=False)
        run(["docker", "exec", name, "mkdir", "-p", "/project"])
        run(["docker", "cp", f"{work}/.", f"{name}:/project"])
        verdict = programs_common.run_phases(
            lambda script, lp, deadline: stream_exec(name, script, lp, deadline),
            log_path, body.timeout_s)
    finally:
        # Harvest artifacts BEFORE removing the sandbox (docker cp works on a
        # stopped container too); a timed-out run may still hold partial output.
        extract_out(name, "/project/output", os.path.join(run_abs, "output"))
        extract_out(name, "/project/.openvisor/usage.json", os.path.join(run_abs, "usage.json"))
        run(["docker", "rm", "-f", name], check=False)
    return programs_common.response(verdict, log_path)


@app.post("/programs/cleanup")
def program_cleanup(body: ProgramCleanupIn):
    """Remove a program sandbox AND its layer-cache volume (instance deleted /
    program deleted). Demos deliberately keep volumes on stop; this is the one
    place a sandbox is fully reclaimed."""
    if not _PROG_NAME_RE.match(body.name):
        raise HTTPException(400, f"invalid program sandbox name '{body.name}'")
    if ORCHESTRATOR == "kubernetes":
        return k8s_backend.program_cleanup(body.name)
    run(["docker", "rm", "-f", body.name], check=False)
    run(["docker", "volume", "rm", f"{body.name}-data"], check=False)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
