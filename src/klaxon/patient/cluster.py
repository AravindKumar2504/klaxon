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
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import klaxon
from klaxon.patient import deploys, stack

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
EVIDENCE = REPO / "evidence" / "week6" / "kind"  # week 5's first run on kind: evidence/week5/kind
# Any of these could aim docker, kind or kubectl at another host, cluster or file.
FOREIGN_TARGETS = (*stack.FOREIGN_TARGETS, "KUBECONFIG", "KIND_CLUSTER_NAME", "KIND_EXPERIMENTAL_PROVIDER")
WAIT_SECONDS = 300  # each workload's wait in up(): Kafka and Grafana's stack are the slowest to start
JOB_POLL_SECONDS = 2.0  # how often up() looks at a Job: its failure is noticed within this, not at the end of the wait
# Week 6, item 3. kind-up applies server-side, as this one manager, and takes back every field another manager set
# (kubectl set env, a fault's patch): the manifests' settings win, as compose.yaml's do at each make up.
FIELD_MANAGER = "klaxon-kind-up"
ACCESS_FILE = "01-klaxon-access.yaml"  # what Klaxon's tools may read on the cluster, and nothing more
READER = "klaxon-reader"  # its ServiceAccount
READER_KUBECONFIG = REPO / ".kind" / "reader.kubeconfig"  # a token of klaxon-reader, made for each run on kind
READER_CONTEXT = f"{CONTEXT}-reader"
TOKEN_SECONDS = 3600  # how long a reader's token lasts: an hour, about four times a gated run
# Week 6, item 4: Klaxon's one write on the cluster, an approved rollback, through an account of its own that can
# change only our four Deployments (02-klaxon-rollback.yaml). Its token, too, is made for each gated run, for an hour.
ROLLBACK_FILE = "02-klaxon-rollback.yaml"
ROLLBACK = "klaxon-rollback"  # its ServiceAccount
ROLLBACK_KUBECONFIG = REPO / ".kind" / "rollback.kubeconfig"
ROLLBACK_CONTEXT = f"{CONTEXT}-rollback"
JOBS = ("job/kafka-init",)  # made again at each kind-up: a Job's template cannot change once it is made
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


def _kubectl(
    *args: str, stdin: str | None = None, timeout: float = 600, config: Path | None = None, context: str = CONTEXT
) -> str:
    # Its discovery cache in .kind/ too: nothing of this cluster goes to ~/.kube.
    config = config or KUBECONFIG
    target = ["--kubeconfig", str(config), "--context", context, "--namespace", NAMESPACE]
    target += ["--cache-dir", str(KUBECONFIG.parent / "cache")]
    return _run(kubectl_binary(), [*target, *args], stdin=stdin, timeout=timeout)


def ensure_cluster_local(config: Path | None = None, context: str = CONTEXT) -> None:
    """Refuse unless this repo's kubeconfig names the cluster's API server on 127.0.0.1, where kind puts it."""
    config = config or KUBECONFIG
    if not config.is_file():
        raise ClusterError(f"no kind cluster here: {_shown(config)} is missing (make kind-up)")
    view = ("config", "view", "--minify", "--output", "jsonpath={.clusters[0].cluster.server}")
    server = _kubectl(*view, config=config, context=context).strip()
    if urlparse(server).hostname != "127.0.0.1":
        raise ClusterError(f"refusing: the cluster's API server is {server!r}, not on this Mac")


def kubectl(*args: str, stdin: str | None = None, timeout: float = 600) -> str:
    """kubectl on this cluster only: its own kubeconfig, the context kind-klaxon, the namespace klaxon, and an API
    server on 127.0.0.1. With the cluster's admin access: for make kind-*, the faults and the harness, never Klaxon's
    tools."""
    ensure_cluster_local()
    return _kubectl(*args, stdin=stdin, timeout=timeout)


def reader_kubectl(*args: str, timeout: float = 600) -> str:
    """kubectl as klaxon-reader, which Kubernetes lets only get, list and watch in the namespace (deploy/kind,
    01-klaxon-access.yaml): what Klaxon's tools read the cluster with. The same checks as kubectl()."""
    ensure_cluster_local(READER_KUBECONFIG, READER_CONTEXT)
    return _kubectl(*args, timeout=timeout, config=READER_KUBECONFIG, context=READER_CONTEXT)


def rollback_kubectl(*args: str, stdin: str | None = None, timeout: float = 600) -> str:
    """kubectl as klaxon-rollback, which Kubernetes lets only get, watch and patch our four Deployments and read their
    ReplicaSets (deploy/kind, 02-klaxon-rollback.yaml): what the approval gate rolls a service back with, after a
    person's typed yes (week 6, item 4). The same checks as kubectl()."""
    ensure_cluster_local(ROLLBACK_KUBECONFIG, ROLLBACK_CONTEXT)
    return _kubectl(*args, stdin=stdin, timeout=timeout, config=ROLLBACK_KUBECONFIG, context=ROLLBACK_CONTEXT)


def _account_kubeconfig(account: str, path: Path, context: str) -> Path:
    """A kubeconfig for one of Klaxon's accounts with a token of its own that lasts TOKEN_SECONDS, written beside the
    cluster's (readable by this user only, never committed), made again each time: the access ends with the token."""
    token = kubectl("create", "token", account, f"--duration={TOKEN_SECONDS}s").strip()
    if not token or any(c.isspace() for c in token):
        raise ClusterError(f"kubectl create token gave no token for {account}")
    view = json.loads(kubectl("config", "view", "--minify", "--raw", "--output", "json"))
    (entry,) = view.get("clusters") or [None]
    server = (entry or {}).get("cluster", {}).get("server", "")
    if urlparse(server).hostname != "127.0.0.1":
        raise ClusterError(f"refusing: the cluster's API server is {server!r}, not on this Mac")
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": CONTEXT, "cluster": entry["cluster"]}],
        "users": [{"name": account, "user": {"token": token}}],
        "contexts": [{"name": context, "context": {"cluster": CONTEXT, "user": account, "namespace": NAMESPACE}}],
        "current-context": context,
    }
    path.parent.mkdir(exist_ok=True)
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(json.dumps(config, indent=2) + "\n")
    return path


def make_reader_kubeconfig() -> Path:
    """klaxon-reader's kubeconfig, made for each run on kind: the tools' access ends with its token."""
    return _account_kubeconfig(READER, READER_KUBECONFIG, READER_CONTEXT)


def make_rollback_kubeconfig() -> Path:
    """klaxon-rollback's kubeconfig, made for each gated run on kind (week 6, item 4): the gate's access to roll a
    service back ends with its token."""
    return _account_kubeconfig(ROLLBACK, ROLLBACK_KUBECONFIG, ROLLBACK_CONTEXT)


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


SERVER_SIDE = ("apply", "--server-side", "--force-conflicts", f"--field-manager={FIELD_MANAGER}")


def manifests(files: tuple[str, ...], image: str = IMAGE) -> str:
    """These manifests as one stream, our image named as given: the same files, with klaxon-patient:dev replaced by
    the tag of the image this kind-up built, so a code change rolls our workloads and none other changes."""
    texts = [(DEPLOY / file).read_text(encoding="utf-8") for file in files]
    return "\n---\n".join(text.replace(f"image: {IMAGE}", f"image: {image}") for text in texts)


def apply(*files: str, image: str = IMAGE) -> None:
    kubectl(*SERVER_SIDE, "--filename", "-", stdin=manifests(files, image))


def wait(workload: str, clock=time.monotonic, sleep=time.sleep) -> None:
    """Wait until a workload is ready (a Deployment's or StatefulSet's rollout) or done (a Job). A Job that failed
    stops the wait at once, with its reason: it is never retried (backoffLimit 0, as compose.yaml runs it once)."""
    limit = f"--timeout={WAIT_SECONDS}s"
    if not workload.startswith("job/"):
        kubectl("rollout", "status", workload, limit, timeout=WAIT_SECONDS + 60)
        return
    deadline = clock() + WAIT_SECONDS
    while True:
        status = json.loads(kubectl("get", workload, "--output", "json")).get("status") or {}
        held = {c.get("type"): c for c in status.get("conditions") or [] if c.get("status") == "True"}
        if "Complete" in held:
            return
        if "Failed" in held or status.get("failed"):
            why = (held.get("Failed") or {}).get("message") or f"{status.get('failed')} pod(s) failed"
            raise ClusterError(f"{workload} failed: {why} (kubectl logs {workload} shows its output)")
        if clock() >= deadline:
            raise ClusterError(f"{workload} did not finish within {WAIT_SECONDS} s (make kind-ps)")
        sleep(JOB_POLL_SECONDS)


# What our image is built from (docker/patient.Dockerfile copies these and nothing else): its tag on kind is their hash.
BUILD_FILES = ("docker/patient.Dockerfile", "pyproject.toml", "requirements/services.lock.txt")


def build_inputs() -> list[Path]:
    """The files our image is built from, in a fixed order: the Dockerfile, pyproject.toml, the lock file, and our
    Python code. Not pip's metadata in src/klaxon.egg-info (written by an editable install, never read by the image's
    own install), nor bytecode."""
    code = sorted(path for path in (REPO / "src").rglob("*.py") if "__pycache__" not in path.parts)
    return [REPO / name for name in BUILD_FILES] + code


def content_tag(inputs: list[Path] | None = None) -> str:
    """Our image, tagged by what it is built from: klaxon-patient:<the first 12 hex digits of their SHA-256>. The same
    files give the same tag, so up() rolls our workloads after a change to them and leaves them be otherwise. Not by
    Docker's image id: on this Mac each build of the same files gave a new one (2026-11-10, six builds, six ids), so
    every kind-up rolled everything. A newer base image under the same tag (python:3.12.15-slim) keeps the tag: the
    pods keep the image they run until a change to our files."""
    digest = hashlib.sha256()
    for path in build_inputs() if inputs is None else inputs:
        digest.update(path.relative_to(REPO).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    tagged = f"{IMAGE.split(':')[0]}:{digest.hexdigest()[:12]}"
    stack._run(["tag", IMAGE, tagged])
    return tagged


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
    image = content_tag()
    _say(f"Loading it into the cluster as {image}, its own id (it is never pulled)")
    _run(kind_binary(), ["load", "docker-image", image, "--name", NAME], timeout=600)
    _say(
        f"Applying {NAMESPACE_FILE}, {ACCESS_FILE}, {ROLLBACK_FILE} and the ConfigMaps of compose.yaml's mounted files"
    )
    apply(NAMESPACE_FILE, ACCESS_FILE, ROLLBACK_FILE)
    kubectl(*SERVER_SIDE, "--filename", "-", stdin=json.dumps(configmaps()))
    for files, waits in STAGES:
        for job in (workload for workload in waits if workload in JOBS):
            kubectl("delete", job, "--ignore-not-found", "--wait=true")  # made again, as Compose runs it at each up
        _say(f"Applying {', '.join(files)}")
        apply(*files, image=image)
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
        run, where=WHERE, title="Klaxon week 6: the money check on kind, while traffic flows"
    )
    _say(printed)
    path = write_evidence(evidence(run, printed, before, pods(), command, now), now)
    _say(f"Evidence: {_shown(path)}")
    return 0 if run.passed else 1


# Deploys on the cluster (week 6, item 3): as klaxon.patient.deploys does them on Compose, in kind's own log.

REVISION = "deployment.kubernetes.io/revision"  # the annotation Kubernetes numbers each rollout with


def kind_log_path() -> Path:
    """kind's own deploy log, apart from Compose's, so a rollback on one is never planned from the other's deploys:
    ~/.klaxon/deploys-kind.jsonl, or the file KLAXON_KIND_DEPLOY_LOG names."""
    named = os.environ.get("KLAXON_KIND_DEPLOY_LOG")
    return Path(named) if named else Path.home() / ".klaxon" / "deploys-kind.jsonl"


class ClusterDeployer:
    """Deploys one of our services on the kind cluster, and logs each deploy, as deploys.Deployer does on Compose and
    with the same rules: one at a time under the log's lock, only known settings, an expect that must still hold.

    A deploy is a rollout: one JSON patch of the Deployment's behavior settings, whose first step makes Kubernetes
    refuse the whole patch if the Deployment changed since it was read (its resourceVersion), then a wait until the
    rollout is done. What runs is the Deployment's current ReplicaSet: a rollout makes a new one, or brings back the
    earlier one whose pod template it matches, as a rollback does. The log names it by the SHA-256 of its uid, a
    64-hex id as a Compose container's is, so the log's lines read the same and a rollback refuses unless the revision
    it was planned on is the one running. The kubectl it is given decides with which access: the cluster's admin for
    the faults (the harness). A kubectl call that fails is a DeployError here (week 6, item 5), so the gate logs it
    as it logs a failed docker call on Compose, instead of the run stopping on it."""

    def __init__(
        self,
        log: deploys.DeployLog | None = None,
        kubectl: Callable[..., str] = kubectl,
        ensure_local: Callable[[], None] = stack.ensure_local,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log = log or deploys.DeployLog(kind_log_path())
        self._kubectl, self._ensure_local, self._clock = kubectl, ensure_local, clock

    def check_local(self) -> None:
        """Docker must talk to this Mac (kind's node runs in it); each kubectl call checks the API server too."""
        self._ensure_local()

    def _get(self, *what: str) -> dict:
        try:
            return json.loads(self._kubectl("get", *what, "--output", "json"))
        except ClusterError as exc:
            raise deploys.DeployError(f"the cluster could not be read: {exc}") from None

    def _current(self, service: str) -> tuple[dict, dict]:
        """The Deployment, and the ReplicaSet that runs it, once every pod is of one revision: ready or not, since a
        bad revision's pods may never be ready, and that is when a rollback is needed (week 6, item 5)."""
        if service not in deploys.SERVICES:
            raise deploys.DeployError(f"no settings are known for the service {service!r}")
        deployment = self._get("deployment", service)
        meta, status = deployment.get("metadata") or {}, deployment.get("status") or {}
        wanted = (deployment.get("spec") or {}).get("replicas", 1)
        done = (
            status.get("observedGeneration", 0) >= meta.get("generation", 1)
            and status.get("updatedReplicas") == wanted
            and status.get("replicas") == wanted  # no pod of an older revision left
        )
        if not done:
            raise deploys.DeployError(f"{service} is not one revision: a rollout is going on")
        revision = (meta.get("annotations") or {}).get(REVISION)
        sets = [
            replica_set
            for replica_set in self._get("replicasets", "--selector", f"app.kubernetes.io/name={service}")["items"]
            if any(
                owner.get("uid") == meta.get("uid") for owner in replica_set["metadata"].get("ownerReferences") or []
            )
            and (replica_set["metadata"].get("annotations") or {}).get(REVISION) == revision
        ]
        if revision is None or len(sets) != 1:
            raise deploys.DeployError(f"{service}'s current revision could not be found")
        return deployment, sets[0]

    @staticmethod
    def _settings(service: str, template: Mapping) -> dict[str, str] | None:
        (container,) = template["spec"]["containers"]
        environment = {item["name"]: item["value"] for item in container.get("env") or [] if "value" in item}
        return deploys.settings_of(service, environment)

    def _running_of(self, service: str, current: Mapping) -> deploys.Running:
        identity = hashlib.sha256(str(current["metadata"]["uid"]).encode()).hexdigest()
        return deploys.Running(identity, self._settings(service, current["spec"]["template"]))

    def running(self, service: str) -> deploys.Running:
        """The service's running revision and its settings. Reads only."""
        _, current = self._current(service)
        return self._running_of(service, current)

    def deploy(
        self, service: str, settings: Mapping[str, str], by: str, expect: deploys.Expect | None = None
    ) -> deploys.Deploy | None:
        """Roll the service out with these settings, wait until the rollout is done, and log the deploy, all under
        the deploy lock; with expect, only if the service still runs that revision with those settings and the log's
        latest line for it is that one (else DeployChanged, and nothing is done). None, with nothing done, when it
        already runs them. A service whose revision cannot be read is deployed all the same, as on Compose."""
        wanted = deploys.check_settings(service, settings)
        self.check_local()
        with self.log.locked():
            try:  # one read: the patch tests the very resourceVersion the expect was checked on (week 6, item 5)
                deployment, current = self._current(service)
                before: deploys.Running | None = self._running_of(service, current)
            except deploys.DeployError:
                deployment, before = self._get("deployment", service), None
            if expect is not None:
                latest = self.log.latest(service)
                if (
                    before is None
                    or before.container != expect.container
                    or before.settings != expect.settings
                    or latest is None
                    or latest.seq != expect.seq
                ):
                    raise deploys.DeployChanged(f"{service} changed since it was looked at: nothing was deployed")
            if before is not None and before.settings == wanted:
                return None
            patch = self._patch(service, deployment, wanted)
            try:
                self._kubectl("patch", "deployment", service, "--type", "json", "--patch", patch)
            except ClusterError:
                # Refused whole, or applied with its answer lost (week 6, item 5): read it again to know which.
                now = self._get("deployment", service)
                if self._settings(service, now["spec"]["template"]) != wanted:
                    read = deployment["metadata"]["resourceVersion"]
                    if now["metadata"].get("resourceVersion") != read:
                        raise deploys.DeployChanged(
                            f"{service} changed since it was read: nothing was deployed"
                        ) from None
                    raise deploys.DeployError(
                        f"{service}'s rollout was refused by the cluster: nothing was deployed"
                    ) from None
            status = ("rollout", "status", f"deployment/{service}", f"--timeout={WAIT_SECONDS}s")
            try:
                self._kubectl(*status, timeout=WAIT_SECONDS + 60)
            except ClusterError as exc:  # changed, but not seen done: not logged, as a failed `compose up --wait`
                raise deploys.DeployError(f"{service} was patched, but its rollout was not seen done: {exc}") from None
            after = self.running(service)
            record = self.log.append(
                at=deploys.utc_now(self._clock),
                service=service,
                by=" ".join(by.split())[: deploys.MAX_BY] or "unknown",
                container=after.container,
                settings=after.settings,
                replaced_container=None if before is None else before.container,
                replaced_settings=None if before is None else before.settings,
            )
        if after.settings != wanted:  # logged as it happened, then said
            raise deploys.DeployError(
                f"{service} was rolled out, but its new revision does not run the settings asked for"
            )
        return record

    @staticmethod
    def _patch(service: str, deployment: Mapping, wanted: Mapping[str, str]) -> str:
        """The JSON patch: first a test of the Deployment's resourceVersion, then one replace per behavior setting,
        at its place in the container's environment. Each setting must be there already, as the manifests set it."""
        version = deployment["metadata"]["resourceVersion"]
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        places = {item["name"]: n for n, item in enumerate(container.get("env") or [])}
        steps: list[dict] = [{"op": "test", "path": "/metadata/resourceVersion", "value": version}]
        for name, (inside, _) in deploys.SERVICES[service].items():
            if inside not in places:
                raise deploys.DeployError(f"{service}'s manifest does not set {inside}: nothing was deployed")
            path = f"/spec/template/spec/containers/0/env/{places[inside]}/value"
            steps.append({"op": "replace", "path": path, "value": wanted[name]})
        return json.dumps(steps)


def klaxons_deployer() -> ClusterDeployer:
    """The deployer the approval gate rolls a service back with on kind (week 6, item 4): kind's own deploy log, the
    faults' too, and its lock, so a rollback is planned from the deploy it undoes and waits for any other; and
    klaxon-rollback's access, never the admin's. Putting back the settings before the bad deploy makes the same pod
    template as the revision before it, so Kubernetes brings that revision back, as kubectl rollout undo does, but
    only if the Deployment is unchanged since it was read: the patch's test of its resourceVersion."""
    return ClusterDeployer(kubectl=rollback_kubectl)


HEALTH_KINDS = "deployments,statefulsets,pods"  # what verification reads of the cluster, as klaxon-reader
HEALTH_READ_SECONDS = 30.0  # one read of verification's, as its docker reads on Compose (verify.DOCKER_TIMEOUT)


def health_objects() -> list[dict]:
    """The namespace's Deployments, StatefulSets and pods, as klaxon-reader reads them: what verification judges the
    cluster's health by (klaxon.agent.verify.cluster_unwell). Reads only."""
    found = json.loads(reader_kubectl("get", HEALTH_KINDS, "--output", "json", timeout=HEALTH_READ_SECONDS))
    items = found.get("items") if isinstance(found, dict) else None
    if not isinstance(items, list):
        raise ClusterError("the cluster's workloads came back in an unexpected shape")
    return items


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
    commands.add_parser("reader", help="a token of klaxon-reader for Klaxon's tools, an hour long, in .kind/")
    checking = commands.add_parser("check", help="the money check while traffic flows, saved to evidence/week6/kind")
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
        elif args.command == "reader":
            path = make_reader_kubeconfig()
            _say(f"A token of {READER} for Klaxon's tools, for {TOKEN_SECONDS // 60} minutes: {_shown(path)}.")
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
