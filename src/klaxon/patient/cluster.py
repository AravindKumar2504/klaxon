"""The patient on a local kind cluster: Kubernetes in Docker, on this Mac (week 5, item 5).

The same services as compose.yaml, from our own manifests in deploy/kind: the official images, the same settings, and
our services' image, built here by Compose and loaded into the cluster, never pulled. kind maps the same ports as the
Compose stack on 127.0.0.1 (Aravind's choice, 2026-11-08), so traffic, the money check and Grafana work unchanged:
the two take turns, never both up. up() starts everything in compose.yaml's depends_on order and waits at each step.

Every command acts only on this repo's own cluster, on this Mac's Docker. kind always gets the cluster's name, and
kind and kubectl always get the cluster's own kubeconfig file (.kind/kubeconfig in the repo, not committed):
~/.kube/config is never read or written. kubectl always gets the context kind-klaxon and the namespace klaxon, and
runs only once the cluster's API server is known to be on 127.0.0.1. Variables that could point docker, kind or
kubectl elsewhere are dropped. up() refuses unless docker talks to a local socket and the Compose stack is down.
down() deletes the cluster with its data, which is synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import klaxon
from klaxon.patient import stack

if TYPE_CHECKING:  # the traffic and the check need the service libraries; up, down and ps do not
    from klaxon.demo import under_traffic

REPO = stack.REPO
DEPLOY = REPO / "deploy" / "kind"
CONFIG = DEPLOY / "cluster.yaml"
NAME = "klaxon"
CONTEXT = f"kind-{NAME}"
NAMESPACE = "klaxon"
KUBECONFIG = REPO / ".kind" / "kubeconfig"
IMAGE = "klaxon-patient:dev"  # compose.yaml's name for our services' image
NODE = f"{NAME}-control-plane"  # the node's container, as kind names it
EVIDENCE = REPO / "evidence" / "week5" / "kind"
# Any of these could aim docker, kind or kubectl at another host, cluster or file.
FOREIGN_TARGETS = (*stack.FOREIGN_TARGETS, "KUBECONFIG", "KIND_CLUSTER_NAME", "KIND_EXPERIMENTAL_PROVIDER")
WAIT_SECONDS = 300  # each workload's wait in up(): Kafka and Grafana's stack are the slowest to start
# The money check's addresses can come from these (klaxon.checks.live). On the cluster they must be its defaults,
# the ports deploy/kind/cluster.yaml maps, or the check could read another stack while its run says kind.
ADDRESS_VARIABLES = ("KLAXON_READONLY_DSN", "KLAXON_PROVIDER_URL", "KLAXON_KAFKA_BOOTSTRAP")

# What up() applies after the namespace, in compose.yaml's depends_on order, and what it waits for after each step:
# first what the others need, then the topics, then our four services.
NAMESPACE_FILE = "00-namespace.yaml"
STAGES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("10-postgres.yaml", "10-redis.yaml", "10-kafka.yaml", "10-otel-lgtm.yaml", "10-fake-provider.yaml"),
        (
            "statefulset/postgres",
            "deployment/redis",
            "statefulset/kafka",
            "statefulset/otel-lgtm",
            "statefulset/fake-provider",
        ),
    ),
    (("20-kafka-init.yaml",), ("job/kafka-init",)),
    (
        ("30-webhook-receiver.yaml", "30-payments-api.yaml", "30-ledger-consumer.yaml", "30-payment-confirmer.yaml"),
        (
            "deployment/webhook-receiver",
            "deployment/payments-api",
            "deployment/ledger-consumer",
            "deployment/payment-confirmer",
        ),
    ),
)
# The files compose.yaml mounts into containers, given to the cluster as ConfigMaps: the same files, so the two
# stacks never differ.
CONFIG_FILES = {
    "postgres-initdb": ("docker/postgres/initdb/10-roles.sh", "docker/postgres/initdb/20-schema.sql"),
    "grafana-files": (
        "docker/grafana/datasources.yaml",
        "docker/grafana/dashboards.yaml",
        "docker/grafana/klaxon-patient.json",
    ),
}

SCENARIO = "kind-check-under-traffic"
WHERE = "this Mac's kind cluster (Kubernetes in Docker, from deploy/kind)"
LABEL = (
    "Real run on this Mac's kind cluster (Kubernetes in Docker, from our own manifests in deploy/kind), with synthetic"
    " traffic from klaxon.traffic (made-up customers and amounts, a simulated provider, no real money). The scenario"
    " of make check-under-traffic on the Compose stack: the money check Klaxon will use to verify fixes, run again and"
    " again while traffic flows, then once on the quiet cluster. It is not the Klaxon agent: no model was called."
)


class ClusterError(RuntimeError):
    """The cluster could not be used as asked. Nothing was done past the step that failed."""


def _binary(name: str, *places: Path) -> str:
    found = shutil.which(name)
    if found:
        return found
    for place in places:
        if place.exists():
            return str(place)
    raise ClusterError(f"{name} not found")


def kind_binary() -> str:
    return _binary("kind", Path("/opt/homebrew/bin/kind"), Path("/usr/local/bin/kind"))


def kubectl_binary() -> str:
    return _binary("kubectl", Path.home() / ".docker" / "bin" / "kubectl")  # Docker Desktop's own


def _run(binary: str, args: list[str], *, stdin: str | None = None, timeout: float = 600) -> str:
    env = {name: value for name, value in os.environ.items() if name not in FOREIGN_TARGETS} | {
        "DOCKER_CLI_HINTS": "false",
        # kind calls docker by name: Docker Desktop's folder goes on PATH too.
        "PATH": os.pathsep.join([str(Path.home() / ".docker" / "bin"), os.environ.get("PATH", "")]),
    }
    done = subprocess.run(
        [binary, *args], cwd=REPO, env=env, input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if done.returncode != 0:
        raise ClusterError(f"{Path(binary).name} {' '.join(args)} failed ({done.returncode}): {done.stderr[-2000:]}")
    return done.stdout


def _kubectl(*args: str, stdin: str | None = None, timeout: float = 600) -> str:
    # Its discovery cache in .kind/ too: nothing of this cluster goes to ~/.kube.
    target = ["--kubeconfig", str(KUBECONFIG), "--context", CONTEXT, "--namespace", NAMESPACE]
    target += ["--cache-dir", str(KUBECONFIG.parent / "cache")]
    return _run(kubectl_binary(), [*target, *args], stdin=stdin, timeout=timeout)


def ensure_cluster_local() -> None:
    """Refuse unless this repo's kubeconfig names the cluster's API server on 127.0.0.1, where kind puts it."""
    if not KUBECONFIG.is_file():
        raise ClusterError(f"no kind cluster here: {_shown(KUBECONFIG)} is missing (make kind-up)")
    server = _kubectl("config", "view", "--minify", "--output", "jsonpath={.clusters[0].cluster.server}").strip()
    if urlparse(server).hostname != "127.0.0.1":
        raise ClusterError(f"refusing: the cluster's API server is {server!r}, not on this Mac")


def kubectl(*args: str, stdin: str | None = None, timeout: float = 600) -> str:
    """kubectl on this cluster only: its own kubeconfig, the context kind-klaxon, the namespace klaxon, and an API
    server on 127.0.0.1."""
    ensure_cluster_local()
    return _kubectl(*args, stdin=stdin, timeout=timeout)


def clusters() -> list[str]:
    """The kind clusters on this Mac's Docker, by name. Reads only."""
    return _run(kind_binary(), ["get", "clusters"]).split()


def configmaps() -> dict:
    """The files compose.yaml mounts, as ConfigMaps in one List: each file's text exactly as in the repo."""
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app.kubernetes.io/part-of": "klaxon"}},
                "data": {Path(file).name: (REPO / file).read_text(encoding="utf-8") for file in files},
            }
            for name, files in CONFIG_FILES.items()
        ],
    }


def apply(*files: str) -> None:
    kubectl("apply", *(part for file in files for part in ("--filename", str(DEPLOY / file))))


def wait(workload: str) -> None:
    """Wait until a workload is ready (a Deployment's or StatefulSet's rollout) or done (a Job)."""
    limit = f"--timeout={WAIT_SECONDS}s"
    if workload.startswith("job/"):
        kubectl("wait", "--for=condition=complete", workload, limit, timeout=WAIT_SECONDS + 60)
    else:
        kubectl("rollout", "status", workload, limit, timeout=WAIT_SECONDS + 60)


def compose_running() -> list[str]:
    """The Compose stack's running services: they hold the ports the cluster maps."""
    return sorted(str(c.get("Service", "?")) for c in stack.containers() if c.get("State") == "running")


def _shown(path: Path) -> str:
    """A path as printed: from the repo's folder when it is inside it."""
    return path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)


def _say(text: str) -> None:
    print(text, flush=True)


def up() -> None:
    """Create the cluster if it is not there, build our image and load it, then apply the manifests in compose.yaml's
    order and wait at each step. The Compose stack must be down: the two use the same ports."""
    stack.ensure_local()
    running = compose_running()
    if running:
        raise ClusterError(f"the Compose stack is up ({', '.join(running)}), on the same ports: make down first")
    KUBECONFIG.parent.mkdir(exist_ok=True)
    if NAME not in clusters():
        _say(f"Creating the kind cluster {NAME} (one node; about a minute the first time)")
        create = ["create", "cluster", "--name", NAME, "--config", str(CONFIG), "--kubeconfig", str(KUBECONFIG)]
        _run(kind_binary(), [*create, "--wait", "120s"], timeout=900)
    elif not KUBECONFIG.is_file():  # the cluster is there, its file is not (another checkout made it): write it again
        _run(kind_binary(), ["export", "kubeconfig", "--name", NAME, "--kubeconfig", str(KUBECONFIG)])
    ensure_cluster_local()
    _say(f"Building our image, {IMAGE}, as compose.yaml builds it")
    stack.compose("build", "fake-provider")  # one service's build makes the image all five of ours use
    _say(f"Loading {IMAGE} into the cluster (it is never pulled)")
    _run(kind_binary(), ["load", "docker-image", IMAGE, "--name", NAME], timeout=600)
    _say(f"Applying {NAMESPACE_FILE} and the ConfigMaps of compose.yaml's mounted files")
    apply(NAMESPACE_FILE)
    kubectl("apply", "--filename", "-", stdin=json.dumps(configmaps()))
    for files, waits in STAGES:
        _say(f"Applying {', '.join(files)}")
        apply(*files)
        for workload in waits:
            _say(f"  waiting for {workload}")
            wait(workload)
    _say("The patient is up on kind: every workload is ready (make kind-ps; make kind-check).")


def down() -> None:
    """Delete the cluster, with its data (synthetic): the next up() starts from empty books."""
    stack.ensure_local()
    if NAME not in clusters():
        _say(f"No kind cluster named {NAME}: nothing to delete.")
        return
    _run(kind_binary(), ["delete", "cluster", "--name", NAME, "--kubeconfig", str(KUBECONFIG)], timeout=300)
    _say(f"Deleted the kind cluster {NAME} and its data.")


def pods() -> list[dict]:
    """Each pod: its phase, whether its containers are ready, their restarts, images and image digests. Only the
    status is read: a pod's spec, and so its environment, never leaves this function."""
    out = []
    for pod in json.loads(kubectl("get", "pods", "--output", "json"))["items"]:
        status = pod.get("status") or {}
        containers = status.get("containerStatuses") or []
        out.append(
            {
                "name": pod["metadata"]["name"],
                "phase": status.get("phase"),
                "ready": bool(containers) and all(c.get("ready") for c in containers),
                "restarts": sum(int(c.get("restartCount", 0)) for c in containers),
                "images": [c.get("image") for c in containers],
                "image_ids": [c.get("imageID") for c in containers],
            }
        )
    return sorted(out, key=lambda pod: pod["name"])


def describe() -> dict:
    """Versions, the node's image and the pods, for evidence files. Reads only."""
    versions = json.loads(kubectl("version", "--output", "json"))
    return {
        "kind": _run(kind_binary(), ["version"]).strip(),
        "kubectl_client": (versions.get("clientVersion") or {}).get("gitVersion"),
        "kubernetes_server": (versions.get("serverVersion") or {}).get("gitVersion"),
        "node_image": stack._run(["inspect", "--type", "container", "--format", "{{.Config.Image}}", NODE]).strip(),
        "pods": pods(),
    }


MAPPING = re.compile(r'containerPort: (\d+), hostPort: (\d+), listenAddress: "([^"]*)"')
PUBLISHED = re.compile(r"^(\d+)/tcp -> 127\.0\.0\.1:(\d+)$", re.MULTILINE)


def port_mappings() -> list[tuple[int, int, str]]:
    """Each (node port, this Mac's port, the address it listens on) that deploy/kind/cluster.yaml maps."""
    text = CONFIG.read_text(encoding="utf-8")
    return sorted((int(node), int(host), address) for node, host, address in MAPPING.findall(text))


def ensure_ports_are_the_clusters() -> None:
    """Refuse unless this cluster's node publishes each port of deploy/kind/cluster.yaml on 127.0.0.1, to its node
    port: then whatever answers on those ports is the cluster, not the Compose stack."""
    try:
        published = {(int(node), int(host)) for node, host in PUBLISHED.findall(stack._run(["port", NODE]))}
    except stack.StackError as exc:
        raise ClusterError(f"the cluster's node is not running (make kind-up): {exc.stderr.strip()[-300:]}") from None
    missing = [(node, host) for node, host, _ in port_mappings() if (node, host) not in published]
    if missing:
        node, host = missing[0]
        raise ClusterError(f"the cluster's node does not publish 127.0.0.1:{host} to {node}: make kind-up")


def ready() -> None:
    """Refuse unless the money check's addresses are its defaults, the Compose stack is down, the ports are the
    cluster's, and every pod is ready (or done)."""
    stack.ensure_local()
    moved = [name for name in ADDRESS_VARIABLES if os.environ.get(name)]
    if moved:
        raise ClusterError(f"{moved[0]} is set: the money check would read another address than the cluster's")
    running = compose_running()
    if running:
        raise ClusterError(f"the Compose stack is up ({', '.join(running)}), on the cluster's ports: make down")
    ensure_ports_are_the_clusters()
    waiting = [pod["name"] for pod in pods() if not pod["ready"] and pod["phase"] != "Succeeded"]
    if waiting:
        raise ClusterError(f"not ready: {', '.join(waiting)} (make kind-ps)")


def fingerprint() -> str:
    """SHA-256 over the files that define the cluster and our image, beside the code's own fingerprint."""
    digest = hashlib.sha256()
    files = [REPO / "Makefile", REPO / "pyproject.toml"]
    files += sorted(
        p for folder in ("deploy", "docker", "requirements") for p in (REPO / folder).rglob("*") if p.is_file()
    )
    for path in files:
        digest.update(path.relative_to(REPO).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def provenance(command: str, now: datetime, cluster: dict) -> dict:
    """When, where and from exactly which code and manifests a run came."""
    from klaxon.demo.duplicate_webhook import code_fingerprint

    def git(*args: str) -> str | None:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=5)
        return done.stdout.strip() if done.returncode == 0 else None

    status = git("status", "--porcelain", "--", "src", "deploy", "docker", "requirements", "Makefile", "pyproject.toml")
    return {
        "rerun_command": command,
        "ran_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "ran_at_local": now.astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "klaxon_version": klaxon.__version__,
        "code_sha256": code_fingerprint(),
        "cluster_files_sha256": fingerprint(),
        "git_commit": git("rev-parse", "HEAD"),
        "git_clean_code_and_cluster": None if status is None else status == "",
        "cluster": cluster,
    }


def evidence(
    run: under_traffic.UnderTrafficRun, printed: str, before: dict, after: list[dict], command: str, now: datetime
) -> dict:
    return {
        "label": LABEL,
        "scenario": SCENARIO,
        **provenance(command, now, before),
        "pods_after": after,
        "every_check_passed": run.passed,
        "traffic_settings": asdict(run.settings),
        "warmup_seconds": run.warmup_seconds,
        "counted": run.counted(),
        "checks": [asdict(c) for c in run.checks],
        "quiet_check": asdict(run.quiet),
        "traffic": run.traffic,
        "provider_behavior": run.behavior_note,
        "printed_output": printed.splitlines(),
    }


def write_evidence(record: dict, now: datetime) -> Path:
    """The run's evidence, in a file of its own: never over another."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    settings = record["traffic_settings"]
    stem = f"{now.astimezone(timezone.utc):%Y-%m-%dT%H%M%SZ}_{SCENARIO}_rate{settings['rate']:g}_seed{settings['seed']}"
    for n in range(1, 100):
        path = EVIDENCE / (f"{stem}.json" if n == 1 else f"{stem}-{n}.json")
        try:
            with path.open("x", encoding="utf-8") as out:
                out.write(json.dumps(record, indent=2, default=str) + "\n")
        except FileExistsError:
            continue
        return path
    raise ClusterError(f"no free name for {stem}.json")


def check(rate: float, checks: int, seed: int, command: str) -> int:
    """The money check again and again while traffic flows on the cluster, then once on the quiet cluster; saved."""
    from klaxon.demo import under_traffic

    ready()
    before = describe()
    now = datetime.now(timezone.utc)
    run = under_traffic.run_live(rate=rate, checks=checks, seed=seed)
    printed = under_traffic.render(
        run, where=WHERE, title="Klaxon week 5: the money check on kind, while traffic flows"
    )
    _say(printed)
    path = write_evidence(evidence(run, printed, before, pods(), command, now), now)
    _say(f"Evidence: {_shown(path)}")
    return 0 if run.passed else 1


def _ps() -> None:
    rows = pods()
    if not rows:
        _say("No pods in the namespace klaxon.")
    for pod in rows:
        ready_word = "ready" if pod["ready"] else ("done" if pod["phase"] == "Succeeded" else "NOT READY")
        images = ", ".join(str(image) for image in pod["images"])
        _say(f"{pod['name']:<40} {ready_word:<10} {pod['phase'] or '-':<10} restarts {pod['restarts']:<3} {images}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m klaxon.patient.cluster", description="The patient on a local kind cluster (week 5, item 5)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="create the cluster, build and load our image, start in order, wait")
    commands.add_parser("down", help="delete the cluster and its data (synthetic)")
    commands.add_parser("ps", help="the pods: ready, phase, restarts, images")
    checking = commands.add_parser("check", help="the money check while traffic flows, saved to evidence/week5/kind")
    checking.add_argument("--checks", type=int, default=20)
    checking.add_argument("--rate", type=float, default=5.0)
    checking.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            up()
        elif args.command == "down":
            down()
        elif args.command == "ps":
            _ps()
        else:
            command = (
                f"python -m klaxon.patient.cluster check --checks {args.checks} --rate {args.rate:g} --seed {args.seed}"
            )
            return check(args.rate, args.checks, args.seed, command)
    except (ClusterError, stack.StackError, subprocess.TimeoutExpired) as exc:
        print(f"kind: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
