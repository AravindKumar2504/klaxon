"""The local Docker Compose stack, driven from Python. The live demo and the end-to-end tests use it.

Every command acts only on this repo's own Compose project ("klaxon") on this
Mac's Docker: the compose file and project name are always passed explicitly,
environment variables that could point docker somewhere else are dropped, and
up() and reset() refuse to run unless docker talks to a local socket. reset()
deletes the stack's data, which is synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # src/klaxon/patient/stack.py -> the repo root
COMPOSE_FILE = REPO / "compose.yaml"
PROJECT = "klaxon"
# Any of these could aim docker or compose at another host, file or project.
FOREIGN_TARGETS = ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES")


class StackError(RuntimeError):
    pass


def docker() -> str:
    found = shutil.which("docker")
    if found:
        return found
    desktop = Path.home() / ".docker" / "bin" / "docker"  # where Docker Desktop puts it for a user install
    if desktop.exists():
        return str(desktop)
    raise StackError("docker not found: is Docker Desktop installed and running?")


def _run(args: list[str], env: dict[str, str] | None = None, timeout: float = 600) -> str:
    binary = docker()
    full_env = {name: value for name, value in os.environ.items() if name not in FOREIGN_TARGETS} | {
        "DOCKER_CLI_HINTS": "false",
        # Docker calls its credential helper by name, so its folder must be on PATH too.
        "PATH": f"{Path(binary).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    done = subprocess.run(
        [binary, *args], cwd=REPO, env=full_env | (env or {}), capture_output=True, text=True, timeout=timeout
    )
    if done.returncode != 0:
        raise StackError(f"docker {' '.join(args)} failed ({done.returncode}): {done.stderr.strip()[-2000:]}")
    return done.stdout


def compose(*args: str, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    target = ["--file", str(COMPOSE_FILE), "--project-name", PROJECT, "--project-directory", str(REPO)]
    return _run(["compose", *target, *args], env=env, timeout=timeout)


def ensure_local() -> None:
    """Refuse unless docker's current endpoint is a socket on this machine (Docker Desktop's is)."""
    endpoint = _run(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]).strip()
    if not endpoint.startswith(("unix://", "npipe://")):
        raise StackError(f"refusing: docker points at {endpoint!r}, not this machine")


def reset() -> None:
    """Stop the stack and delete its volumes: the next up() starts from empty books."""
    ensure_local()
    compose("down", "--volumes", "--remove-orphans")


def up(handler: str = "idempotent", confirmer: str = "by_our_id") -> None:
    """Build, start, and wait until every service reports healthy."""
    ensure_local()
    settings = {"LEDGER_HANDLER": handler, "CONFIRMER_HANDLER": confirmer}
    # The known-bad "retry forever" setting only ever comes from a fault (klaxon.faults), never from a stray variable.
    settings |= {"LEDGER_BAD_EVENTS": "dead_letter", "CONFIRMER_BAD_EVENTS": "dead_letter"}
    compose("up", "--detach", "--build", "--wait", env=settings)


def start_container(service: str) -> None:
    """Start one stopped service's container, without Compose also starting what it depends on."""
    container = compose("ps", "--all", "--quiet", service).strip()
    if not container:
        raise StackError(f"{service} has no container (make up)")
    _run(["start", container])


def container_env(service: str) -> dict[str, str]:
    """The environment a running service's container actually has (not what compose.yaml says)."""
    container = compose("ps", "--quiet", service).strip()
    if not container:
        raise StackError(f"{service} is not running (make up)")
    pairs = json.loads(_run(["inspect", "--format", "{{json .Config.Env}}", container]))
    return dict(pair.split("=", 1) for pair in pairs)


def describe() -> dict:
    """Versions and images, for evidence files."""
    version = json.loads(_run(["version", "--format", "{{json .}}"]))
    return {
        "docker_client": version.get("Client", {}).get("Version"),
        "docker_server": (version.get("Server") or {}).get("Version"),
        "compose": compose("version", "--short").strip(),
        "images": [
            {
                "service": image.get("ContainerName"),
                "image": f"{image.get('Repository')}:{image.get('Tag')}",
                "id": image.get("ID"),
            }
            for image in json.loads(compose("images", "--format", "json") or "[]")
        ],
    }
