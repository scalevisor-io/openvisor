"""Kubernetes orchestrator for the deployer (ORCHESTRATOR=kubernetes).

Same HTTP contract as the docker mode in main.py, mapped onto cluster resources:
  demo   -> DinD Pod `demo-<id>` (+ per-demo PVC for /var/lib/docker, Service,
            basicAuth Secret + traefik.io Middleware, Gateway API HTTPRoute)
  dev run-> one-shot Job `dev-<id>` mounting the shared workspaces PVC subPath,
            bounded by activeDeadlineSeconds, logs harvested before cleanup.

The workspace is still copied INTO the demo (exec + tar, the docker-cp analogue) -
never volume-mounted - so a demo cannot see other projects' workspaces. Dev-run
Jobs are co-scheduled with this deployer pod (podAffinity on app=deployer) because
both mount the ReadWriteOnce workspaces PVC and RWO block storage binds one node.
"""
import logging
import os
import re
import socket
import tarfile
import tempfile
import time
import uuid

from fastapi import HTTPException

import healthcheck
import programs_common

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

log = logging.getLogger("deployer.k8s")

NAMESPACE = os.environ.get("K8S_NAMESPACE", "openvisor")
WORKSPACES = "/workspaces"
WORKSPACES_PVC = os.environ.get("WORKSPACES_PVC", "workspaces")
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "openvisor")
GATEWAY_SECTION = os.environ.get("GATEWAY_SECTION", "web")
DIND_IMAGE = os.environ.get("DEMO_DIND_IMAGE", "docker:27-dind")
DEMO_VOLUME_SIZE = os.environ.get("DEMO_VOLUME_SIZE", "10Gi")
DEMO_STORAGE_CLASS = os.environ.get("DEMO_STORAGE_CLASS", "")
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "openvisor-runner")
# §egress: the per-run filtering proxy runs the mcp image (mcp/egress_proxy.py).
EGRESS_PROXY_IMAGE = os.environ.get("EGRESS_PROXY_IMAGE", "openvisor-mcp")
EGRESS_PROXY_PORT = 3128
RUNNER_PULL_SECRETS = [s for s in os.environ.get("RUNNER_PULL_SECRETS", "").split(",") if s]
RUNTIME = os.environ.get("DEMO_RUNTIME", "")
# §dev-docker: inner dockerd for dev-run sandboxes - RuntimeClass (Sysbox) when
# configured, privileged-container fallback otherwise (demo DinD parity).
DEV_SANDBOX_DOCKER = os.environ.get("DEV_SANDBOX_DOCKER", "0") == "1"
# Size of the per-run ephemeral PVC backing the dev sandbox's /var/lib/docker.
# Without a volume the inner image builds write into the container layer, count
# against the pod's 8Gi ephemeral-storage limit (_resources), and a repo whose
# compose build exceeds it gets the sandbox EVICTED mid-session.
DEV_DOCKER_VOLUME_SIZE = os.environ.get("DEV_DOCKER_VOLUME_SIZE", "20Gi")

_core = None
_batch = None
_custom = None


def _init():
    global _core, _batch, _custom
    if _core is None:
        config.load_incluster_config()
        _core = client.CoreV1Api()
        _batch = client.BatchV1Api()
        _custom = client.CustomObjectsApi()
    return _core, _batch, _custom


def _stream_core() -> "client.CoreV1Api":
    """Fresh CoreV1Api on its OWN ApiClient for every exec/stream call.
    kubernetes.stream monkey-patches api_client.request during the websocket
    handshake and restores it in a finally; sharing one client across the
    threadpool would let a concurrent normal API call get routed through the
    websocket path (ApiException status=0) or leave the shared client patched
    if two streams interleave their restores. A private client isolates that."""
    _init()
    return client.CoreV1Api(client.ApiClient())


def _mem_quantity(v: str) -> str:
    """Accept docker-style 2g/512m as well as k8s quantities (2Gi)."""
    v = v.strip()
    if v and v[-1] in "gGmM" and v[:-1].isdigit():
        return v[:-1] + v[-1].upper() + "i"
    return v


def _cpu_cores(v: str) -> float:
    """Parse a cpu quantity ('0.5' / '2' / k8s '500m') to cores. Raises on junk."""
    v = v.strip()
    if v.endswith(("m", "M")):
        return float(v[:-1]) / 1000.0
    return float(v)


def _mem_bytes(v: str) -> float:
    """Parse a memory quantity (docker-style 512m/2g, k8s 512Mi/2Gi, plain bytes)
    to bytes. Raises on junk."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG])?i?[bB]?", v.strip())
    if not m:
        raise ValueError(f"bad memory quantity: {v!r}")
    mult = {"k": 1024.0, "m": 1024.0 ** 2, "g": 1024.0 ** 3}
    return float(m.group(1)) * (mult[m.group(2).lower()] if m.group(2) else 1.0)


def _resources(cpu: str, mem: str, cpu_request: str = "", mem_request: str = "") -> dict:
    mem = _mem_quantity(mem)
    # cap ephemeral storage so a runaway inner build (docker graph on the demo's
    # own PVC, but logs/tmp land on node ephemeral) cannot fill the node disk -
    # the classic DinD node-killer that pod memory limits do not cover.
    # Requests default to the historical floor; program sandboxes pass the
    # admin-set per-program requests (docker mode can't honor a cpu request).
    return {
        "requests": {"cpu": cpu_request or "100m",
                     "memory": _mem_quantity(mem_request) if mem_request else "256Mi",
                     "ephemeral-storage": "1Gi"},
        "limits": {"cpu": cpu, "memory": mem, "ephemeral-storage": "8Gi"},
    }


def _not_found(e: ApiException) -> bool:
    return e.status == 404


def _apply_extra_host(pod_spec: dict, extra_host: str) -> None:
    """Add the "host:target" tailnet alias to a sandbox pod spec (§ssh remotes).
    hostAliases only accepts IPs, but extra_host may name a Service (e.g. the
    Tailscale egress proxy, whose pod IP changes on restart), so resolve at
    dispatch time; a literal IP passes through unchanged. Shared by the dev-run
    and program paths - a program cloning a tailnet-only forge needs exactly the
    same alias a dev run does."""
    host, _, target = (extra_host or "").partition(":")
    if not (host and target):
        return
    try:
        ip = socket.gethostbyname(target)
    except OSError as e:
        raise HTTPException(502, f"extra_host target {target!r} does not resolve: {e}")
    pod_spec["hostAliases"] = [{"ip": ip, "hostnames": [host]}]


# --------------------------------------------------------------------------- demos

def demo_pod_name(project_id: str) -> str:
    return f"demo-{project_id}"


def _demo_pod_manifest(name: str, cpu: str, mem: str, ephemeral: bool = False) -> dict:
    container = {
        "name": "dind",
        "image": DIND_IMAGE,
        "env": [{"name": "DOCKER_TLS_CERTDIR", "value": ""}],
        "resources": _resources(cpu, mem),
        "volumeMounts": [{"name": "docker-graph", "mountPath": "/var/lib/docker"}],
    }
    spec = {
        "restartPolicy": "Always",
        # a demo runs untrusted customer code; it must not get a usable
        # kube API token mounted in
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "containers": [container],
        # ephemeral (verify pods): emptyDir instead of a PVC - throwaway boot
        # gate sandboxes shouldn't accrete block storage per project
        "volumes": [{
            "name": "docker-graph",
            **({"emptyDir": {}} if ephemeral else
               {"persistentVolumeClaim": {"claimName": f"{name}-data"}}),
        }],
    }
    if RUNTIME:
        # hardened runtime (e.g. sysbox-runc RuntimeClass): no privileged flag
        spec["runtimeClassName"] = RUNTIME
    else:
        container["securityContext"] = {"privileged": True}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"app": name, "openvisor/demo": "true"}},
        "spec": spec,
    }


def _ensure_demo_pvc(name: str) -> None:
    core, _, _ = _init()
    try:
        core.read_namespaced_persistent_volume_claim(f"{name}-data", NAMESPACE)
        return
    except ApiException as e:
        if not _not_found(e):
            raise
    spec = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": DEMO_VOLUME_SIZE}},
    }
    if DEMO_STORAGE_CLASS:
        spec["storageClassName"] = DEMO_STORAGE_CLASS
    core.create_namespaced_persistent_volume_claim(NAMESPACE, {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"{name}-data", "labels": {"openvisor/demo": "true"}},
        "spec": spec,
    })


def _pod_phase(name: str) -> str:
    core, _, _ = _init()
    try:
        pod = core.read_namespaced_pod(name, NAMESPACE)
    except ApiException as e:
        if _not_found(e):
            return "absent"
        raise
    if pod.metadata.deletion_timestamp:
        return "terminating"
    return pod.status.phase or "Unknown"


def _delete_pod_and_wait(name: str, tries: int = 60) -> None:
    core, _, _ = _init()
    try:
        core.delete_namespaced_pod(name, NAMESPACE, grace_period_seconds=25)
    except ApiException as e:
        if not _not_found(e):
            raise
    for _ in range(tries):
        if _pod_phase(name) == "absent":
            return
        time.sleep(2)
    raise HTTPException(500, f"pod {name} did not terminate in time")


def _ensure_demo_pod(name: str, cpu: str, mem: str) -> None:
    core, _, _ = _init()
    phase = _pod_phase(name)
    if phase == "Running":
        return
    if phase != "absent":
        # Pods cannot be restarted once finished/terminating: recreate (the
        # demo-<id>-data PVC keeps the inner docker cache - fast resume).
        _delete_pod_and_wait(name)
    _ensure_demo_pvc(name)
    core.create_namespaced_pod(NAMESPACE, _demo_pod_manifest(name, cpu, mem))
    for _ in range(120):
        if _pod_phase(name) == "Running":
            return
        time.sleep(2)
    raise HTTPException(500, f"demo pod {name} never reached Running")


def _exec(name: str, command: list[str], timeout: int = 300, container: str | None = None) -> tuple[int, str]:
    """kubectl-exec equivalent; returns (exit_code, combined output)."""
    core = _stream_core()
    try:
        resp = stream(
            core.connect_get_namespaced_pod_exec, name, NAMESPACE,
            command=command, container=container,
            stdin=False, stdout=True, stderr=True, tty=False,
            _preload_content=False, _request_timeout=timeout,
        )
    except ApiException as e:
        raise HTTPException(500, f"exec into {name} failed: {e.reason}")
    out: list[str] = []
    deadline = time.time() + timeout
    while resp.is_open() and time.time() < deadline:
        resp.update(timeout=2)
        if resp.peek_stdout():
            out.append(resp.read_stdout())
        if resp.peek_stderr():
            out.append(resp.read_stderr())
    resp.close()
    # returncode reads the ERROR channel and can raise (TypeError/ApiException)
    # if the stream was closed by our deadline before a status frame arrived
    try:
        rc = resp.returncode
    except Exception:
        rc = None
    return (rc if rc is not None else 1), "".join(out)


def wait_for_inner_docker(name: str, tries: int = 60) -> None:
    for _ in range(tries):
        rc, _out = _exec(name, ["docker", "info"], timeout=20)
        if rc == 0:
            return
        time.sleep(2)
    raise HTTPException(500, f"inner docker in {name} never became ready")


def ensure_compose_plugin(name: str) -> None:
    rc, _ = _exec(name, ["docker", "compose", "version"], timeout=60)
    if rc != 0:
        _exec(name, ["apk", "add", "--no-cache", "docker-cli-compose"], timeout=180)


def sync_workspace(name: str, project_id: str) -> None:
    ws = os.path.join(WORKSPACES, project_id)
    if not os.path.isdir(ws):
        raise HTTPException(400, f"workspace {ws} not found")
    _sync_dir(name, ws)


def _sync_dir(name: str, ws: str) -> None:
    """docker-cp analogue: tar a host dir (this pod mounts the workspaces PVC)
    and unpack it at /project inside the sandbox pod. The exact byte count is
    sent first so the receiver needs no stdin EOF (websocket half-close is
    unreliable). The tar is spooled to a temp file and streamed in chunks so
    peak RSS stays ~one chunk, not ~2x the workspace size (which would
    OOM-kill the small deployer pod on a real workspace)."""
    with tempfile.NamedTemporaryFile(prefix="ws-", suffix=".tar") as tmp:
        with tarfile.open(fileobj=tmp, mode="w") as tar:
            tar.add(ws, arcname=".")
        size = tmp.tell()
        tmp.seek(0)
        script = (
            "rm -rf /project && mkdir -p /project && "
            f"head -c {size} /dev/stdin > /tmp/.ws.tar && "
            "tar -xf /tmp/.ws.tar -C /project && rm -f /tmp/.ws.tar && echo SYNC_DONE"
        )
        core = _stream_core()
        resp = stream(
            core.connect_get_namespaced_pod_exec, name, NAMESPACE,
            command=["sh", "-c", script],
            stdin=True, stdout=True, stderr=True, tty=False,
            _preload_content=False, binary=True,
        )
        out = b""
        try:
            while True:
                chunk = tmp.read(1 << 19)
                if not chunk:
                    break
                resp.write_stdin(chunk)
                resp.update(timeout=0)
            deadline = time.time() + 300
            while resp.is_open() and time.time() < deadline:
                resp.update(timeout=2)
                if resp.peek_stdout():
                    out += resp.read_stdout()
                if resp.peek_stderr():
                    out += resp.read_stderr()
                if b"SYNC_DONE" in out:
                    break
        finally:
            resp.close()
    if b"SYNC_DONE" not in out:
        raise HTTPException(500, f"workspace sync into {name} failed: {out[-500:]!r}")


def compose_up(name: str, port: int, workdir: str = ".") -> None:
    ensure_compose_plugin(name)
    proj = "/project" if workdir in (".", "") else f"/project/{workdir}"
    script = (
        # Same clean-slate as main.py: a redeploy can change the inner compose
        # project name (workdir moves), leaving the old stack holding $PORT.
        # Containers only - images and named volumes stay (fast resume).
        'old=$(docker ps -aq); [ -n "$old" ] && docker rm -f $old; '
        f"cd {proj} && export PORT={int(port)} && "
        "files='-f compose.demo.yml'; "
        "[ -f compose.base.yml ] && files='-f compose.base.yml -f compose.demo.yml'; "
        "docker compose $files up -d --build --force-recreate"
    )
    rc, out = _exec(name, ["sh", "-c", script], timeout=900)
    if rc != 0:
        raise HTTPException(500, f"compose up in {name} failed:\n{out[-800:]}")


def wait_for_app(name: str, port: int, workdir: str) -> None:
    """Readiness gate, same contract as main.py: no HTTPRoute for a demo whose
    app never answers HTTP - fail the start with the inner state + logs."""
    if healthcheck.TIMEOUT_S <= 0:
        return
    _rc, out = _exec(name, ["sh", "-c", healthcheck.probe_script(port, healthcheck.TIMEOUT_S)],
                     timeout=healthcheck.TIMEOUT_S + 60)
    if healthcheck.READY in out:
        return
    _rc, diag = _exec(name, ["sh", "-c", healthcheck.diagnostics_script(port, workdir)],
                      timeout=120)
    raise HTTPException(500, (
        f"demo app did not answer HTTP on port {port} within "
        f"{healthcheck.TIMEOUT_S}s after compose up:\n{diag[-2400:]}"))


def _apply_demo_routing(project_id: str, subdomain: str, port: int, htpasswd: str, domain: str) -> None:
    """Traefik file-provider fragment analogue: Service + basicAuth Secret +
    traefik.io Middleware + Gateway API HTTPRoute, all named demo-<id>[-auth]."""
    core, _, custom = _init()
    name = demo_pod_name(project_id)
    host = f"{subdomain}.{domain}"

    _delete_demo_routing(project_id)

    core.create_namespaced_service(NAMESPACE, {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": {"openvisor/demo": "true"}},
        "spec": {
            "selector": {"app": name},
            "ports": [{"name": "http", "port": port, "targetPort": port}],
        },
    })
    core.create_namespaced_secret(NAMESPACE, {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"{name}-auth", "labels": {"openvisor/demo": "true"}},
        "type": "Opaque",
        "stringData": {"users": htpasswd},
    })
    custom.create_namespaced_custom_object(
        "traefik.io", "v1alpha1", NAMESPACE, "middlewares", {
            "apiVersion": "traefik.io/v1alpha1",
            "kind": "Middleware",
            "metadata": {"name": f"{name}-auth", "labels": {"openvisor/demo": "true"}},
            "spec": {"basicAuth": {"secret": f"{name}-auth"}},
        })
    custom.create_namespaced_custom_object(
        "gateway.networking.k8s.io", "v1", NAMESPACE, "httproutes", {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": name, "labels": {"openvisor/demo": "true"}},
            "spec": {
                "parentRefs": [{"name": GATEWAY_NAME, "sectionName": GATEWAY_SECTION}],
                "hostnames": [host],
                "rules": [{
                    "filters": [{
                        "type": "ExtensionRef",
                        "extensionRef": {"group": "traefik.io", "kind": "Middleware", "name": f"{name}-auth"},
                    }],
                    "backendRefs": [{"name": name, "port": port}],
                }],
            },
        })


def _delete_demo_routing(project_id: str) -> None:
    core, _, custom = _init()
    name = demo_pod_name(project_id)
    for deleter, args in (
        (custom.delete_namespaced_custom_object, ("gateway.networking.k8s.io", "v1", NAMESPACE, "httproutes", name)),
        (custom.delete_namespaced_custom_object, ("traefik.io", "v1alpha1", NAMESPACE, "middlewares", f"{name}-auth")),
        (core.delete_namespaced_secret, (f"{name}-auth", NAMESPACE)),
        (core.delete_namespaced_service, (name, NAMESPACE)),
    ):
        try:
            deleter(*args)
        except ApiException as e:
            if not _not_found(e):
                raise


def demo_start(project_id: str, subdomain: str, port: int, htpasswd: str,
               workdir: str, cpu: str, mem: str, domain: str) -> dict:
    name = demo_pod_name(project_id)
    _ensure_demo_pod(name, cpu, mem)
    wait_for_inner_docker(name)
    sync_workspace(name, project_id)
    compose_up(name, port, workdir)
    wait_for_app(name, port, workdir)
    _apply_demo_routing(project_id, subdomain, port, htpasswd, domain)
    return {"ok": True, "container": name, "state": "running"}


def demo_stop(project_id: str) -> dict:
    name = demo_pod_name(project_id)
    _delete_demo_routing(project_id)
    if _pod_phase(name) != "absent":
        # PVC demo-<id>-data is kept: fast resume, inner images/volumes preserved
        _delete_pod_and_wait(name)
    return {"ok": True, "container": name, "state": "stopped"}


def verify_pod_name(project_id: str) -> str:
    return f"verify-{project_id}"


def demo_verify(project_id: str, port: int, workdir: str, cpu: str, mem: str,
                name: str = "", run_dir: str = "", screenshots: list | None = None) -> dict:
    """§14.5 boot gate, same contract as main.py: throwaway DinD pod (emptyDir,
    no PVC, no Service/HTTPRoute - the probe runs via exec), carrying the
    openvisor/demo label so it inherits the demo NetworkPolicy isolation.
    Always deleted afterwards; ok=false + logs = agent-fixable outcome."""
    name = name or verify_pod_name(project_id)
    core, _, _ = _init()
    if _pod_phase(name) != "absent":
        _delete_pod_and_wait(name)
    core.create_namespaced_pod(NAMESPACE, _demo_pod_manifest(name, cpu, mem, ephemeral=True))
    try:
        for _ in range(120):
            if _pod_phase(name) == "Running":
                break
            time.sleep(2)
        else:
            raise HTTPException(500, f"verify pod {name} never reached Running")
        wait_for_inner_docker(name)
        sync_workspace(name, project_id)
        try:
            compose_up(name, port, workdir)
            wait_for_app(name, port, workdir)
        except HTTPException as exc:
            return {"ok": False, "logs": str(exc.detail)}
        # §After-shots: the inner compose publishes $PORT on the pod interface;
        # browser-mcp's page loads are allowlisted into demo pods by the
        # demo-isolation NetworkPolicy, so the pod IP is photographable for as
        # long as this window stays open.
        shots = []
        if screenshots:
            import browsershot
            pod_ip = core.read_namespaced_pod(name, NAMESPACE).status.pod_ip
            if pod_ip:
                shots = browsershot.capture(f"http://{pod_ip}:{port}", screenshots)
        return {"ok": True, "logs": "", "screenshots": shots}
    finally:
        try:
            _delete_pod_and_wait(name)
        except HTTPException as exc:
            log.warning("verify pod %s cleanup: %s", name, exc.detail)


# --------------------------------------------------------------------------- dev runs

def job_name(project_id: str) -> str:
    return f"dev-{project_id}"


def _delete_job_and_wait(name: str, tries: int = 60) -> None:
    _, batch, _ = _init()
    try:
        batch.delete_namespaced_job(name, NAMESPACE, propagation_policy="Foreground")
    except ApiException as e:
        if not _not_found(e):
            raise
    for _ in range(tries):
        try:
            batch.read_namespaced_job(name, NAMESPACE)
        except ApiException as e:
            if _not_found(e):
                return
            raise
        time.sleep(2)
    raise HTTPException(500, f"job {name} did not terminate in time")


def dev_stop(project_id: str, run_name: str = "") -> dict:
    """§14 stop: delete the project's dev Job (idempotent). The dev_run poll
    treats the vanished Job as "stopped" and its finally reclaims the Secret."""
    _, batch, _ = _init()
    name = run_name or job_name(project_id)
    try:
        batch.read_namespaced_job(name, NAMESPACE)
    except ApiException as e:
        if _not_found(e):
            _stop_egress_proxy(name)  # clean a proxy stranded by a race
            return {"ok": True, "was_running": False}
        raise
    _delete_job_and_wait(name)
    _stop_egress_proxy(name)
    return {"ok": True, "was_running": True}


def _job_pod(name: str):
    core, _, _ = _init()
    pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={name}").items
    return pods[0] if pods else None


def _job_logs(name: str, project_id: str, run_dir: str = "") -> str:
    """Runner tail. Prefer the pod's API logs; fall back to the runner's own
    .openvisor/run.log on the shared workspace (the only source that survives a
    deadline kill, which deletes the pod before we can read its logs)."""
    core, _, _ = _init()
    pod = _job_pod(name)
    if pod:
        try:
            out = core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE, tail_lines=60)
            if out:
                return out
        except ApiException:
            pass
    run_log = os.path.join(WORKSPACES, run_dir or project_id, ".openvisor", "run.log")
    try:
        with open(run_log) as f:
            return f.read()
    except OSError:
        return ""


def _egress_proxy_name(run_name: str) -> str:
    return f"egress-{run_name}"


def _start_egress_proxy(run_name: str, allowlist: list) -> None:
    """§egress (K8s): launch the per-run filtering proxy Pod + Service and wait
    for it Ready. The runner's NetworkPolicy makes this proxy its sole internet
    route, so the run can't start until the proxy is up. Idempotent."""
    import json
    core, _, _ = _init()
    name = _egress_proxy_name(run_name)
    _stop_egress_proxy(run_name)  # clear any stale prior instance
    labels = {"app": name, "openvisor/egress-proxy": "true"}
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "containers": [{
                "name": "proxy",
                "image": EGRESS_PROXY_IMAGE,
                "command": ["python", "egress_proxy.py"],
                "env": [{"name": "EGRESS_ALLOWLIST", "value": json.dumps(list(allowlist))},
                        {"name": "EGRESS_PROXY_PORT", "value": str(EGRESS_PROXY_PORT)}],
                "ports": [{"containerPort": EGRESS_PROXY_PORT}],
                "resources": {"requests": {"cpu": "25m", "memory": "64Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
                "readinessProbe": {"tcpSocket": {"port": EGRESS_PROXY_PORT},
                                   "initialDelaySeconds": 2, "periodSeconds": 2},
            }],
        },
    }
    if RUNNER_PULL_SECRETS:
        pod["spec"]["imagePullSecrets"] = [{"name": s} for s in RUNNER_PULL_SECRETS]
    core.create_namespaced_pod(NAMESPACE, pod)
    core.create_namespaced_service(NAMESPACE, {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "labels": labels},
        "spec": {"selector": {"app": name},
                 "ports": [{"port": EGRESS_PROXY_PORT, "targetPort": EGRESS_PROXY_PORT}]},
    })
    deadline = time.time() + 90
    while time.time() < deadline:
        p = core.read_namespaced_pod(name, NAMESPACE)
        if any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])):
            return
        if p.status.phase in ("Failed", "Succeeded"):
            break
        time.sleep(2)
    raise HTTPException(502, "egress proxy did not become ready")


def _stop_egress_proxy(run_name: str) -> None:
    """Tear down the per-run proxy Pod + Service (best-effort, idempotent)."""
    core, _, _ = _init()
    name = _egress_proxy_name(run_name)
    for delete in (lambda: core.delete_namespaced_service(name, NAMESPACE),
                   lambda: core.delete_namespaced_pod(name, NAMESPACE)):
        try:
            delete()
        except ApiException as e:
            if not _not_found(e):
                raise


def _egress_env(run_name: str) -> list:
    """HTTP(S)_PROXY pointing at the per-run proxy, and NO_PROXY for the in-cluster
    hosts the runner must reach DIRECTLY (tools + cluster DNS namespaces)."""
    proxy = f"http://{_egress_proxy_name(run_name)}:{EGRESS_PROXY_PORT}"
    no_proxy = ("localhost,127.0.0.1,::1,context7,browser-mcp,websearch-mcp,"
                ".svc,.svc.cluster.local,.cluster.local")
    return [
        {"name": "HTTP_PROXY", "value": proxy}, {"name": "http_proxy", "value": proxy},
        {"name": "HTTPS_PROXY", "value": proxy}, {"name": "https_proxy", "value": proxy},
        {"name": "NO_PROXY", "value": no_proxy}, {"name": "no_proxy", "value": no_proxy},
    ]


def dev_run(body, cpu: str, mem: str) -> dict:
    """Blocking facade over a one-shot Job (same response contract as docker mode).
    activeDeadlineSeconds enforces the timeout in-cluster; we also keep a local
    deadline as backstop. LLM_API_KEY travels via a Secret, not a plain env var."""
    core, batch, _ = _init()
    name = body.run_name or job_name(body.project_id)
    rel = body.run_dir or body.project_id
    if not os.path.isdir(os.path.join(WORKSPACES, rel)):
        raise HTTPException(400, f"workspace for {rel} not found")

    # §dev-pod resources: per-project scheduling requests ride the body. A request
    # above the instance limit raises this run's limit to match (requests <= limits
    # must hold or the API rejects the Job); an unparseable value fails open to the
    # instance defaults rather than killing the build.
    cpu_req, mem_req = body.cpu_request, body.mem_request
    try:
        if cpu_req and _cpu_cores(cpu_req) > _cpu_cores(cpu):
            cpu = cpu_req
    except ValueError:
        log.warning("dev_run %s: ignoring bad cpu_request %r", name, cpu_req)
        cpu_req = ""
    try:
        if mem_req and _mem_bytes(mem_req) > _mem_bytes(mem):
            mem = mem_req
    except ValueError:
        log.warning("dev_run %s: ignoring bad mem_request %r", name, mem_req)
        mem_req = ""

    _delete_job_and_wait(name)
    try:
        core.delete_namespaced_secret(f"{name}-env", NAMESPACE)
    except ApiException as e:
        if not _not_found(e):
            raise
    core.create_namespaced_secret(NAMESPACE, {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"{name}-env", "labels": {"openvisor/dev-run": "true"}},
        "type": "Opaque",
        "stringData": {"LLM_API_KEY": body.llm_api_key},
    })

    env = [
        {"name": "LLM_MODEL", "value": body.llm_model},
        {"name": "LLM_BASE_URL", "value": body.llm_base_url},
        {"name": "AGENT_BRANCH", "value": body.agent_branch},
        {"name": "GIT_PUSH", "value": "1" if body.git_push else "0"},
        {"name": "GIT_REMOTE_URL", "value": body.remote_url},
        {"name": "GIT_DEFAULT_BRANCH", "value": body.default_branch},
        {"name": "GIT_USER_NAME", "value": body.git_author_name},
        {"name": "GIT_USER_EMAIL", "value": body.git_author_email},
        {"name": "BRAND_NAME", "value": body.brand_name},
        {"name": "GIT_PROVIDER", "value": body.provider},
        # §glab api host: the API base, which need not be the host git dials.
        {"name": "GITLAB_HOST", "value": body.gitlab_host},
        {"name": "LLM_MAX_ITERATIONS", "value": str(body.max_iterations)},
        {"name": "SKIP_AGENT", "value": "1" if body.skip_agent else "0"},
        {"name": "PLAN_ONLY", "value": "1" if body.plan_only else "0"},
        {"name": "LLM_REASONING_EFFORT", "value": body.reasoning_effort or ""},
        # Last path segment only (the run id): OpenAI caps prompt_cache_key at
        # 64 chars and the full devruns/<pid>/<rid> path is 85 - it 400s EVERY
        # call of the build.
        {"name": "LLM_CACHE_KEY", "value": f"dev-{rel.rsplit('/', 1)[-1]}"},
    ]
    # §egress: on lockdown, stand up the per-run filtering proxy first and route
    # the runner's HTTP(S) egress through it; the job pod is labelled so the
    # devrun-egress-locked NetworkPolicy caps it to DNS + tools + Tailscale + proxy.
    # §sandbox git preflight: a distinct label on every Job so each dispatch gets
    # a NEW CNI security identity. Cilium encodes that identity in the packet
    # mark ((id << 16) | magic) and the Tailscale egress proxy routes
    # fwmark 0x80000/0xff0000 out of its tailnet table, so one identity in 256
    # silently severs the sandbox from the git remote for that pod's whole life.
    # Without this, re-dispatching a run under the same name inherits the same
    # identity and retries straight back into the same severed path.
    pod_labels = {"app": name, "openvisor/dev-run": "true",
                  "openvisor/dispatch": uuid.uuid4().hex[:12]}
    if getattr(body, "egress_locked", False):
        _start_egress_proxy(name, body.egress_allowlist or [])
        env += _egress_env(name)
        pod_labels["openvisor/egress-locked"] = "true"
    pod_spec = {
        "restartPolicy": "Never",
        # the runner executes the agent's (untrusted) build; deny it a kube token
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        # co-locate with the deployer pod: both mount the RWO workspaces PVC, so
        # they must land on the same node (spec.nodeName != the hostname label on
        # some clusters, so anchor on the deployer pod, not a nodeSelector)
        "affinity": {
            "podAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [{
                    "labelSelector": {"matchLabels": {"app": "deployer"}},
                    "topologyKey": "kubernetes.io/hostname",
                }],
            },
        },
        "containers": [{
            "name": "runner",
            "image": RUNNER_IMAGE,
            "env": env,
            "envFrom": [{"secretRef": {"name": f"{name}-env"}}],
            "resources": _resources(cpu, mem, cpu_req, mem_req),
            "volumeMounts": [{
                "name": "workspaces",
                "mountPath": "/workspace",
                "subPath": rel,
            }],
        }],
        "volumes": [{
            "name": "workspaces",
            "persistentVolumeClaim": {"claimName": WORKSPACES_PVC},
        }],
    }
    if DEV_SANDBOX_DOCKER:
        # §dev-docker: the runner entrypoint starts an inner dockerd. Sysbox
        # RuntimeClass makes that safe unprivileged (how demo DinD pods run);
        # without one, fall back to a privileged runner container - the same
        # local-dev-only posture as the demo fallback.
        env.append({"name": "DEV_DOCKER", "value": "1"})
        if RUNTIME:
            pod_spec["runtimeClassName"] = RUNTIME
        else:
            pod_spec["containers"][0]["securityContext"] = {"privileged": True}
        # The inner docker graph gets its own per-run generic ephemeral PVC
        # (born and deleted with the pod, like the demo verify pods' throwaway
        # graph but on real block storage): image builds no longer write into
        # the container layer, so they stop counting against the pod's 8Gi
        # ephemeral-storage limit - which otherwise EVICTS the sandbox in the
        # middle of any repo whose compose build is bigger than the cap.
        claim: dict = {"accessModes": ["ReadWriteOnce"],
                       "resources": {"requests": {"storage": DEV_DOCKER_VOLUME_SIZE}}}
        if DEMO_STORAGE_CLASS:
            claim["storageClassName"] = DEMO_STORAGE_CLASS
        pod_spec["containers"][0]["volumeMounts"].append(
            {"name": "docker-graph", "mountPath": "/var/lib/docker"})
        pod_spec["volumes"].append({
            "name": "docker-graph",
            "ephemeral": {"volumeClaimTemplate": {
                "metadata": {"labels": {"openvisor/dev-run": "true"}},
                "spec": claim,
            }},
        })
    if RUNNER_PULL_SECRETS:
        pod_spec["imagePullSecrets"] = [{"name": s} for s in RUNNER_PULL_SECRETS]
    _apply_extra_host(pod_spec, body.extra_host)

    try:
        batch.create_namespaced_job(NAMESPACE, {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": {"openvisor/dev-run": "true"}},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": body.timeout_s,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": pod_labels},
                    "spec": pod_spec,
                },
            },
        })

        deadline = time.time() + body.timeout_s + 60
        timed_out, failed_reason = True, ""
        while time.time() < deadline:
            try:
                job = batch.read_namespaced_job(name, NAMESPACE)
            except ApiException as e:
                if _not_found(e):  # externally killed via /dev/stop
                    timed_out, failed_reason = False, "stopped"
                    break
                raise
            s = job.status
            if s.succeeded:
                timed_out = False
                break
            for cond in (s.conditions or []):
                if cond.type == "Failed" and cond.status == "True":
                    timed_out = cond.reason == "DeadlineExceeded"
                    failed_reason = cond.reason or "Failed"
                    break
            else:
                time.sleep(5)
                continue
            break

        logs = _job_logs(name, body.project_id, rel)[-4000:]
        exit_code = "unknown"
        pod = _job_pod(name)
        if pod:
            for cs in (pod.status.container_statuses or []):
                if cs.state and cs.state.terminated is not None:
                    exit_code = str(cs.state.terminated.exit_code)
    finally:
        # always reclaim the Job and the Secret holding LLM_API_KEY, even if the
        # long poll raised (transient API error mid-run must not leak either)
        try:
            _delete_job_and_wait(name)
        finally:
            try:
                core.delete_namespaced_secret(f"{name}-env", NAMESPACE)
            except ApiException as e:
                if not _not_found(e):
                    raise
            # §egress: reclaim the per-run proxy (no-op when it was never created)
            _stop_egress_proxy(name)

    if timed_out:
        return {"ok": False, "exit_code": "timeout", "timed_out": True, "logs": logs}
    if failed_reason and exit_code == "unknown":
        # e.g. image pull failure: no container ever ran
        return {"ok": False, "exit_code": failed_reason, "timed_out": False, "logs": logs}
    return {"ok": exit_code == "0", "exit_code": exit_code, "timed_out": False, "logs": logs}


# --------------------------------------------------------------------------- programs (§28)

def _program_pod_manifest(name: str, cpu: str, mem: str, cpu_request: str,
                          mem_request: str, ephemeral: bool,
                          extra_host: str = "") -> dict:
    """DinD pod for one program run: the demo manifest shape carrying the
    program label (own NetworkPolicy) and the admin-set per-program resource
    requests+limits. Instance sandboxes keep a {name}-data PVC (docker layer
    cache across runs); admin check sandboxes are emptyDir throwaways."""
    manifest = _demo_pod_manifest(name, cpu, mem, ephemeral=ephemeral)
    manifest["metadata"]["labels"] = {"app": name, "openvisor/program": "true"}
    manifest["spec"]["containers"][0]["resources"] = _resources(
        cpu, mem, cpu_request, mem_request)
    _apply_extra_host(manifest["spec"], extra_host)
    return manifest


def _ensure_program_pvc(name: str) -> None:
    core, _, _ = _init()
    try:
        core.read_namespaced_persistent_volume_claim(f"{name}-data", NAMESPACE)
        return
    except ApiException as e:
        if not _not_found(e):
            raise
    spec = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": DEMO_VOLUME_SIZE}},
    }
    if DEMO_STORAGE_CLASS:
        spec["storageClassName"] = DEMO_STORAGE_CLASS
    core.create_namespaced_persistent_volume_claim(NAMESPACE, {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"{name}-data", "labels": {"openvisor/program": "true"}},
        "spec": spec,
    })


def _exec_logged(name: str, script: str, log_path: str, deadline: float) -> int | None:
    """_exec variant streaming combined output into log_path as it arrives (the
    API serves that file live). Returns the exit code, or None once the
    wall-clock deadline passes (the caller deletes the pod, which also kills
    the inner containers)."""
    core = _stream_core()
    try:
        resp = stream(
            core.connect_get_namespaced_pod_exec, name, NAMESPACE,
            command=["sh", "-c", script],
            stdin=False, stdout=True, stderr=True, tty=False,
            _preload_content=False,
            _request_timeout=max(deadline - time.time(), 5),
        )
    except ApiException as e:
        raise HTTPException(500, f"exec into {name} failed: {e.reason}")
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        while resp.is_open():
            if time.time() > deadline:
                resp.close()
                return None
            resp.update(timeout=2)
            if resp.peek_stdout():
                lf.write(resp.read_stdout())
                lf.flush()
            if resp.peek_stderr():
                lf.write(resp.read_stderr())
                lf.flush()
    try:
        rc = resp.returncode
    except Exception:
        rc = None
    return rc if rc is not None else 1


def _extract_program_artifacts(name: str, run_abs: str) -> None:
    """Reverse of _sync_dir, best-effort: tar output/ + usage.json inside the
    sandbox, stream the bytes out (spooled to a temp file, not RAM), unpack
    under the run dir with a traversal guard. Never raises - a timed-out or
    failed run may still hold partial artifacts, and the caller must tear the
    pod down regardless."""
    script = ('cd /project 2>/dev/null || exit 0; members=""; '
              '[ -d output ] && members="output"; '
              '[ -f .openvisor/usage.json ] && members="$members .openvisor/usage.json"; '
              '[ -n "$members" ] && tar -cf - $members; exit 0')
    try:
        core = _stream_core()
        resp = stream(
            core.connect_get_namespaced_pod_exec, name, NAMESPACE,
            command=["sh", "-c", script],
            stdin=False, stdout=True, stderr=True, tty=False,
            _preload_content=False, binary=True,
        )
        with tempfile.TemporaryFile(prefix="prog-out-") as tmp:
            deadline = time.time() + 300
            while resp.is_open() and time.time() < deadline:
                resp.update(timeout=2)
                if resp.peek_stdout():
                    tmp.write(resp.read_stdout())
                if resp.peek_stderr():
                    resp.read_stderr()  # discard
            resp.close()
            if tmp.tell() == 0:
                return
            tmp.seek(0)
            root = os.path.realpath(run_abs)
            with tarfile.open(fileobj=tmp, mode="r") as tar:
                for member in tar.getmembers():
                    rel = member.name
                    if rel == "output" or rel.startswith("output/"):
                        dest = os.path.realpath(os.path.join(root, rel))
                    elif rel == ".openvisor/usage.json":
                        dest = os.path.join(root, "usage.json")
                    else:
                        continue
                    if dest != root and not dest.startswith(root + os.sep):
                        continue
                    if member.isdir():
                        os.makedirs(dest, exist_ok=True)
                    elif member.isfile():
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        src = tar.extractfile(member)
                        if src is not None:
                            with open(dest, "wb") as f:
                                f.write(src.read())
    except Exception as exc:  # noqa: BLE001 - best-effort harvest
        log.warning("program artifact extraction from %s failed: %s", name, exc)


def program_run(body, run_abs: str, work: str, log_path: str) -> dict:
    """§28, same contract as docker mode: throwaway DinD pod running the shared
    build/deploy/run phases with the log streamed onto the workspaces PVC, then
    artifacts harvested and the pod deleted (the instance PVC survives as layer
    cache)."""
    core, _, _ = _init()
    name = body.name
    ephemeral = name.startswith("progchk-")
    if _pod_phase(name) != "absent":
        _delete_pod_and_wait(name)
    if not ephemeral:
        _ensure_program_pvc(name)
    core.create_namespaced_pod(NAMESPACE, _program_pod_manifest(
        name, body.cpu_limit, body.mem_limit, body.cpu_request, body.mem_request,
        ephemeral, body.extra_host))
    try:
        for _ in range(120):
            if _pod_phase(name) == "Running":
                break
            time.sleep(2)
        else:
            raise HTTPException(500, f"program pod {name} never reached Running")
        wait_for_inner_docker(name)
        ensure_compose_plugin(name)
        _sync_dir(name, work)
        verdict = programs_common.run_phases(
            lambda script, lp, deadline: _exec_logged(name, script, lp, deadline),
            log_path, body.timeout_s)
    finally:
        _extract_program_artifacts(name, run_abs)
        try:
            _delete_pod_and_wait(name)
        except HTTPException as exc:
            log.warning("program pod %s cleanup: %s", name, exc.detail)
    return programs_common.response(verdict, log_path)


def program_cleanup(name: str) -> dict:
    """Remove a program sandbox pod AND its layer-cache PVC (instance/program
    deleted) - the one place a sandbox is fully reclaimed."""
    core, _, _ = _init()
    if _pod_phase(name) != "absent":
        _delete_pod_and_wait(name)
    try:
        core.delete_namespaced_persistent_volume_claim(f"{name}-data", NAMESPACE)
    except ApiException as e:
        if not _not_found(e):
            raise
    return {"ok": True}
