"""Program-run (§28) pieces shared by the docker (main.py) and kubernetes
(k8s.py) backends, so both execute the exact same phases the same way."""
import time

# Every phase sources the worker-written env file INSIDE the sandbox shell -
# model keys must never travel as exec args (command lines land in deployer
# logs and `docker inspect`; same trust boundary as the dev secrets.env).
ENV_PREFIX = "set -a; . /project/.openvisor/program.env 2>/dev/null; set +a; cd /project && "

# (phase, script): build and deploy failures are DOCKER-level (they fail an
# admin "Check Program run"); the run phase's exit code belongs to the program.
PHASES = [
    ("build", "docker compose build"),
    ("deploy", "docker compose config -q && docker compose create program"),
    ("run", "docker compose run --rm program"),
]


def append_log(log_path: str, text: str) -> None:
    with open(log_path, "a", encoding="utf-8", errors="replace") as f:
        f.write(text)


def run_phases(exec_fn, log_path: str, timeout_s: int) -> dict:
    """Drive the shared phases against a sandbox. exec_fn(script, log_path,
    deadline) runs a shell script inside it, streaming output to log_path, and
    returns the exit code or None past the deadline. The wall clock covers the
    WHOLE sequence - a slow build eats into the run budget by design."""
    deadline = time.time() + timeout_s
    build_ok = deploy_ok = False
    exit_code = "unknown"
    timed_out = False
    for phase, script in PHASES:
        append_log(log_path, f"\n===== {phase} =====\n")
        code = exec_fn(ENV_PREFIX + script, log_path, deadline)
        if code is None:
            timed_out = True
            append_log(log_path, f"\n===== TIMEOUT after {timeout_s}s (during {phase}) =====\n")
            break
        if phase == "build":
            build_ok = code == 0
        elif phase == "deploy":
            deploy_ok = code == 0
        else:
            exit_code = str(code)
        if code != 0 and phase != "run":
            append_log(log_path, f"\n===== {phase} failed (exit {code}) =====\n")
            exit_code = str(code)
            break
    return {"build_ok": build_ok, "deploy_ok": deploy_ok,
            "exit_code": "timeout" if timed_out else exit_code, "timed_out": timed_out}


def response(verdict: dict, log_path: str) -> dict:
    """The /programs/run response contract, identical across backends."""
    tail = ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-4000:]
    except OSError:
        pass
    ok = (verdict["build_ok"] and verdict["deploy_ok"]
          and not verdict["timed_out"] and verdict["exit_code"] == "0")
    return {"ok": ok, **verdict, "logs": tail}
