"""The patient on kind (week 5, item 5): our manifests against compose.yaml, and the module that drives the cluster.

Offline. The manifests are read as YAML (PyYAML, a dev tool: Aravind's choice, 2026-11-08) and compared with
compose.yaml setting by setting, so the two stacks never drift apart. The module's commands go to a made-up kind,
kubectl and docker that only record them: nothing here runs a cluster, and every run in these tests is made up.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from klaxon import traffic
from klaxon.demo import under_traffic
from klaxon.patient import cluster, stack

COMPOSE = yaml.safe_load((cluster.REPO / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]
FILES = sorted(path.name for path in cluster.DEPLOY.glob("[0-9]*.yaml"))
DOCS = {name: [doc for doc in yaml.safe_load_all((cluster.DEPLOY / name).read_text()) if doc] for name in FILES}
OBJECTS = [doc for docs in DOCS.values() for doc in docs]
KIND = yaml.safe_load(cluster.CONFIG.read_text())
SECRET = next(o for o in OBJECTS if o["kind"] == "Secret")
WORKLOADS = {o["metadata"]["name"]: o for o in OBJECTS if o["kind"] in ("Deployment", "StatefulSet", "Job")}
SERVICE_OBJECTS = {o["metadata"]["name"]: o for o in OBJECTS if o["kind"] == "Service"}
OURS = "klaxon-patient:dev"


def resolved(value: object) -> str:
    """A compose.yaml value as Compose gives it when no variable is set: ${NAME:-default} is its default."""
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", str(value))


def pod(name: str) -> dict:
    return WORKLOADS[name]["spec"]["template"]["spec"]


def container(name: str) -> dict:
    (only,) = pod(name)["containers"]
    return only


def environment(name: str) -> dict[str, str]:
    """A container's environment, each Secret reference read from the Secret's own manifest."""
    out = {}
    for item in container(name).get("env", []):
        if "value" in item:
            out[item["name"]] = item["value"]
        else:
            ref = item["valueFrom"]["secretKeyRef"]
            assert ref["name"] == SECRET["metadata"]["name"]
            out[item["name"]] = SECRET["stringData"][ref["key"]]
    return out


def seconds(text: str) -> int:
    return int(re.fullmatch(r"(\d+)s", text).group(1))


def probe_command(test: list[str]) -> list[str]:
    """A compose.yaml health check's test as a probe runs it: CMD as is, CMD-SHELL through sh -c."""
    form, *rest = test
    if form == "CMD":
        return rest
    assert form == "CMD-SHELL"
    (line,) = rest
    return ["sh", "-c", line]


# The manifests against compose.yaml


def test_each_compose_service_is_one_workload_of_the_same_name():
    assert sorted(WORKLOADS) == sorted(SERVICES)
    assert len([o for o in OBJECTS if o["kind"] in ("Deployment", "StatefulSet", "Job")]) == len(SERVICES)


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_each_runs_compose_yamls_image_command_and_environment(name):
    compose, ours = SERVICES[name], container(name)
    assert ours["image"] == compose["image"]
    # Compose's command replaces the image's CMD, as args does here; its entrypoint the ENTRYPOINT, as command does.
    assert ours.get("args") == compose.get("command")
    assert ours.get("command") == compose.get("entrypoint")
    assert environment(name) == {key: resolved(value) for key, value in (compose.get("environment") or {}).items()}


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_our_image_is_never_pulled_and_each_other_image_is_pinned(name):
    ours = container(name)
    if ours["image"] == OURS:
        assert ours["imagePullPolicy"] == "Never"
    else:
        assert "imagePullPolicy" not in ours  # a pinned tag's default: pulled once, when not present
        assert re.fullmatch(r"\d[\w.-]*", ours["image"].rsplit(":", 1)[1])  # a version, never "latest"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_service_variables_and_init_as_compose_yaml_has_it(name):
    assert pod(name)["enableServiceLinks"] is False  # no SERVICE_HOST-style variables: the environment is compose's
    assert pod(name).get("shareProcessNamespace", False) == SERVICES[name].get("init", False)


@pytest.mark.parametrize("name", sorted(n for n in SERVICES if "healthcheck" in SERVICES[n]))
def test_each_health_check_carries_over_and_restarts_nothing(name):
    check, ours = SERVICES[name]["healthcheck"], container(name)
    interval, timeout = seconds(check["interval"]), seconds(check["timeout"])
    start_period, start_interval = seconds(check["start_period"]), seconds(check["start_interval"])
    startup, readiness = ours["startupProbe"], ours["readinessProbe"]
    assert startup["exec"]["command"] == readiness["exec"]["command"] == probe_command(check["test"])
    assert (readiness["periodSeconds"], readiness["timeoutSeconds"]) == (interval, timeout)
    assert readiness["failureThreshold"] == check["retries"]
    # As often as Compose checks while a service starts, for as long as Compose waits before it calls it unhealthy.
    assert (startup["periodSeconds"], startup["timeoutSeconds"]) == (start_interval, timeout)
    assert startup["failureThreshold"] == math.ceil((start_period + interval * check["retries"]) / start_interval)
    assert "livenessProbe" not in ours  # Compose never restarts a container for failing its check


def test_a_service_without_a_health_check_has_no_probe():
    for name in SERVICES:
        if "healthcheck" not in SERVICES[name]:
            assert not {"startupProbe", "readinessProbe", "livenessProbe"} & set(container(name))


@pytest.mark.parametrize("name", sorted(n for n in SERVICES if SERVICES[n].get("volumes")))
def test_each_volume_carries_over(name):
    mounts = {m["mountPath"]: m for m in container(name)["volumeMounts"]}
    claims = {c["metadata"]["name"] for c in WORKLOADS[name]["spec"].get("volumeClaimTemplates", [])}
    volumes = {v["name"]: v for v in pod(name).get("volumes", [])}
    assert len(mounts) == len(SERVICES[name]["volumes"])
    for entry in SERVICES[name]["volumes"]:
        source, target, *mode = entry.split(":")
        mount = mounts[target]
        assert mount.get("readOnly", False) == (mode == ["ro"])
        if source.startswith("./"):  # a file or folder of the repo: a ConfigMap of the same files
            path = cluster.REPO / source
            files = cluster.CONFIG_FILES[volumes[mount["name"]]["configMap"]["name"]]
            if path.is_dir():
                assert "subPath" not in mount
                inside = [p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")]
                assert sorted(files) == sorted(p.relative_to(cluster.REPO).as_posix() for p in inside)
            else:
                assert mount["subPath"] == path.name and path.relative_to(cluster.REPO).as_posix() in files
        else:  # a named volume: a volume claim of the same name, in a StatefulSet
            assert WORKLOADS[name]["kind"] == "StatefulSet"
            assert mount["name"] == source and source in claims and source not in volumes


def test_each_init_script_is_executable_exactly_when_it_is_in_the_repo():
    # The image runs an executable script and sources the others: the difference matters.
    (volume,) = [v for v in pod("postgres")["volumes"] if v["name"] == "initdb"]
    items = {item["key"]: item for item in volume["configMap"]["items"]}
    assert sorted(items) == sorted(Path(file).name for file in cluster.CONFIG_FILES["postgres-initdb"])
    for file in cluster.CONFIG_FILES["postgres-initdb"]:
        item = items[Path(file).name]
        assert item["path"] == Path(file).name
        assert bool(item["mode"] & 0o111) == bool((cluster.REPO / file).stat().st_mode & 0o100)


def test_the_config_maps_hold_the_repo_files_exactly():
    made = cluster.configmaps()
    assert made["kind"] == "List"
    by_name = {item["metadata"]["name"]: item for item in made["items"]}
    assert sorted(by_name) == sorted(cluster.CONFIG_FILES)
    for name, files in cluster.CONFIG_FILES.items():
        assert by_name[name]["metadata"]["namespace"] == cluster.NAMESPACE
        assert by_name[name]["data"] == {Path(file).name: (cluster.REPO / file).read_bytes().decode() for file in files}


def test_this_macs_ports_are_compose_yamls_each_on_127_0_0_1_through_a_node_port():
    mappings = KIND["nodes"][0]["extraPortMappings"]
    assert all(m["listenAddress"] == "127.0.0.1" and m["protocol"] == "TCP" for m in mappings)
    assert KIND["networking"]["apiServerAddress"] == "127.0.0.1"
    expected = set()
    for name, compose in SERVICES.items():
        for entry in compose.get("ports", []):
            address, host, target = entry.split(":")
            assert address == "127.0.0.1"
            host_service = SERVICE_OBJECTS[f"{name}-host"]
            assert host_service["spec"]["type"] == "NodePort"
            assert host_service["spec"]["selector"] == WORKLOADS[name]["spec"]["selector"]["matchLabels"]
            (port,) = [p for p in host_service["spec"]["ports"] if p["targetPort"] == int(target)]
            expected.add((port["nodePort"], int(host)))
    assert {(m["containerPort"], m["hostPort"]) for m in mappings} == expected  # every compose port, and no other
    assert len(mappings) == len(expected)
    with_ports = {f"{n}-host" for n in SERVICES if SERVICES[n].get("ports")}
    assert {n for n in SERVICE_OBJECTS if n.endswith("-host")} == with_ports
    # The module reads the same mappings from the file (it has no YAML reader of its own).
    assert cluster.port_mappings() == sorted((node, host, "127.0.0.1") for node, host in expected)


NAMED = re.compile(r"\b(" + "|".join(map(re.escape, sorted(SERVICES, key=len, reverse=True))) + r"):(\d+)\b")


def test_each_name_and_port_a_service_dials_answers_inside_the_cluster():
    dialed = set()
    for name in SERVICES:
        for text in [*environment(name).values(), *(container(name).get("args") or [])]:
            dialed |= {(service, int(port)) for service, port in NAMED.findall(text)}
    assert {("kafka", 9093), ("otel-lgtm", 4318), ("postgres", 5432)} <= dialed  # the controller dials itself
    for service, port in dialed:
        found = SERVICE_OBJECTS[service]
        assert found["spec"]["selector"] == WORKLOADS[service]["spec"]["selector"]["matchLabels"]
        assert port in [p["port"] for p in found["spec"]["ports"]]
        assert port in [p["containerPort"] for p in container(service)["ports"]]


def test_kafka_resolves_to_its_own_pod_before_it_is_ready():
    spec = SERVICE_OBJECTS["kafka"]["spec"]
    assert spec["clusterIP"] == "None" and spec["publishNotReadyAddresses"] is True


def test_every_object_is_in_the_namespace_and_part_of_klaxon():
    for obj in OBJECTS:
        if obj["kind"] != "Namespace":
            assert obj["metadata"]["namespace"] == cluster.NAMESPACE
        assert obj["metadata"]["labels"]["app.kubernetes.io/part-of"] == "klaxon"
    for name, workload in WORKLOADS.items():
        assert workload["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == name
        if workload["kind"] != "Job":
            assert workload["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": name}


def test_what_compose_runs_once_runs_once_and_the_rest_keep_one_copy_running():
    for name, compose in SERVICES.items():
        workload = WORKLOADS[name]
        if compose.get("restart") == "no":
            assert workload["kind"] == "Job" and workload["spec"]["backoffLimit"] == 0
            assert pod(name)["restartPolicy"] == "Never"
        else:
            assert workload["kind"] in ("Deployment", "StatefulSet") and workload["spec"]["replicas"] == 1


def test_the_secret_holds_exactly_what_the_specs_read_and_no_spec_holds_one_itself():
    used = {
        item["valueFrom"]["secretKeyRef"]["key"]
        for name in SERVICES
        for item in container(name).get("env", [])
        if "valueFrom" in item
    }
    assert used == set(SECRET["stringData"])
    secrets = set(SECRET["stringData"].values())
    assert not [item for name in SERVICES for item in container(name).get("env", []) if item.get("value") in secrets]


def test_up_applies_each_manifest_once_in_compose_yamls_order_and_waits_for_each_workload():
    staged = [cluster.NAMESPACE_FILE] + [file for files, _ in cluster.STAGES for file in files]
    assert sorted(staged) == FILES and len(set(staged)) == len(staged)
    assert {o["kind"] for o in DOCS[cluster.NAMESPACE_FILE]} == {"Namespace", "Secret"}
    stage_of = {}
    for n, (files, waits) in enumerate(cluster.STAGES):
        inside = [
            f"{o['kind'].lower()}/{o['metadata']['name']}"
            for file in files
            for o in DOCS[file]
            if o["kind"] in ("Deployment", "StatefulSet", "Job")
        ]
        assert sorted(waits) == sorted(inside)
        stage_of |= {workload.split("/")[1]: n for workload in waits}
    for name, compose in SERVICES.items():
        for needed in compose.get("depends_on", {}):
            assert stage_of[needed] < stage_of[name], f"{name} needs {needed} first"


# The module, against a made-up kind, kubectl and docker

SERVER = "https://127.0.0.1:61234"


class Fake:
    """Made-up kind, kubectl and docker for these tests: each command recorded, each answer scripted. Not the real
    tools, and nothing here runs a cluster."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, list[str], str | None]] = []
        self.clusters = ""  # what `kind get clusters` prints
        self.server = SERVER
        self.compose_up: list[dict] = []
        self.published = "".join(f"{node}/tcp -> 127.0.0.1:{host}\n" for node, host, _ in cluster.port_mappings())
        self.pods = [made_up_pod("payments-api-5d9c7-x2v4q", ready=True)]
        self.docker_local = True
        self.kubeconfig = tmp_path / "kubeconfig"
        monkeypatch.setattr(cluster, "KUBECONFIG", self.kubeconfig)
        monkeypatch.setattr(cluster, "EVIDENCE", tmp_path / "evidence")
        monkeypatch.setattr(cluster, "_run", self.run)
        monkeypatch.setattr(cluster, "kind_binary", lambda: "/made-up/kind")
        monkeypatch.setattr(cluster, "kubectl_binary", lambda: "/made-up/kubectl")
        monkeypatch.setattr(stack, "ensure_local", self.ensure_local)
        monkeypatch.setattr(stack, "containers", lambda timeout=600: list(self.compose_up))
        monkeypatch.setattr(stack, "compose", self.compose)
        monkeypatch.setattr(stack, "_run", self.docker)

    def ensure_local(self, timeout: float = 600) -> None:
        self.calls.append(("docker", ["context", "inspect"], None))
        if not self.docker_local:
            raise stack.StackError("refusing: docker points at 'tcp://10.0.0.9:2376', not this machine")

    def compose(self, *args: str, env: dict | None = None, timeout: float = 600) -> str:
        self.calls.append(("compose", list(args), None))
        return ""

    def docker(self, args: list[str], env: dict | None = None, timeout: float = 600) -> str:
        self.calls.append(("docker", list(args), None))
        if args[0] == "port":
            return self.published
        if args[0] == "inspect":
            return "kindest/node:v0.0.0-made-up\n"
        return ""

    def run(self, binary: str, args: list[str], *, stdin: str | None = None, timeout: float = 600) -> str:
        tool = Path(binary).name
        self.calls.append((tool, list(args), stdin))
        if tool == "kind":
            if args[:2] == ["get", "clusters"]:
                return self.clusters
            if args[:2] in (["create", "cluster"], ["export", "kubeconfig"]):
                self.kubeconfig.write_text("made up")
                self.clusters = "klaxon\n"
            if args[0] == "version":
                return "kind v0.0.0-made-up\n"
            return ""
        assert tool == "kubectl"
        rest = args[6:]
        if rest[:2] == ["config", "view"]:
            return self.server
        if rest[:2] == ["get", "pods"]:
            return json.dumps({"items": self.pods})
        if rest[:1] == ["version"]:
            return json.dumps(
                {"clientVersion": {"gitVersion": "v0.0-made-up"}, "serverVersion": {"gitVersion": "v0.1"}}
            )
        return ""

    def kubectl_calls(self) -> list[list[str]]:
        return [args for tool, args, _ in self.calls if tool == "kubectl"]

    def actions(self) -> list[str]:
        """What was done, in order, without the API server checks before each kubectl call."""
        out = []
        for tool, args, stdin in self.calls:
            if tool == "kubectl":
                rest = args[6:]
                if rest[:2] != ["config", "view"]:
                    out.append(" ".join(["kubectl", *rest]) + (" <stdin" if stdin else ""))
            else:
                out.append(" ".join([tool, *args]))
        return out


def made_up_pod(name: str, ready: bool, phase: str = "Running") -> dict:
    """A pod as `kubectl get pods --output json` describes one; made up, with a made-up password in its spec."""
    return {
        "metadata": {"name": name},
        "spec": {
            "containers": [{"name": "c", "env": [{"name": "DATABASE_URL", "value": "made-up-password-not-real"}]}]
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {"ready": ready, "restartCount": 0, "image": OURS, "imageID": "sha256:made-up"},
            ],
        },
    }


@pytest.fixture
def fake(tmp_path, monkeypatch):
    for name in ("KUBECONFIG", "KIND_EXPERIMENTAL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    return Fake(tmp_path, monkeypatch)


def test_kubectl_always_names_this_clusters_file_context_and_namespace(fake):
    fake.kubeconfig.write_text("made up")
    cluster.kubectl("get", "pods")
    calls = fake.kubectl_calls()
    assert len(calls) == 2  # the API server's check, then the command
    target = ["--kubeconfig", str(fake.kubeconfig), "--context", "kind-klaxon", "--namespace", "klaxon"]
    assert all(args[:6] == target for args in calls)
    assert calls[0][6:8] == ["config", "view"] and calls[1][6:] == ["get", "pods"]


@pytest.mark.parametrize(
    "server", ["https://10.1.2.3:6443", "https://localhost:6443", "https://127.0.0.1.example.com:6443", ""]
)
def test_kubectl_refuses_a_cluster_whose_api_server_is_not_on_this_mac(fake, server):
    fake.kubeconfig.write_text("made up")
    fake.server = server
    with pytest.raises(cluster.ClusterError, match="not on this Mac"):
        cluster.kubectl("delete", "namespace", "klaxon")
    assert [args[6:8] for args in fake.kubectl_calls()] == [["config", "view"]]  # nothing else ran


def test_kubectl_refuses_without_this_clusters_own_kubeconfig(fake):
    with pytest.raises(cluster.ClusterError, match="make kind-up"):
        cluster.kubectl("get", "pods")
    assert fake.calls == []


def test_the_tools_never_see_a_variable_that_points_elsewhere(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cluster.subprocess, "run", run)
    for name in cluster.FOREIGN_TARGETS:
        monkeypatch.setenv(name, "made-up-elsewhere")
    cluster._run("/made-up/kubectl", ["get", "pods"])
    assert not set(cluster.FOREIGN_TARGETS) & set(seen["env"])
    assert {"KUBECONFIG", "KIND_EXPERIMENTAL_PROVIDER", "DOCKER_HOST", "DOCKER_CONTEXT"} <= set(cluster.FOREIGN_TARGETS)
    assert seen["env"]["PATH"].startswith(str(Path.home() / ".docker" / "bin"))  # kind finds Docker Desktop's docker


def test_up_refuses_while_the_compose_stack_holds_the_ports_and_makes_nothing(fake):
    fake.compose_up = [{"Service": "payments-api", "State": "running"}, {"Service": "kafka-init", "State": "exited"}]
    with pytest.raises(cluster.ClusterError, match=r"Compose stack is up \(payments-api\).*make down first"):
        cluster.up()
    assert fake.actions() == ["docker context inspect"]


def test_up_refuses_a_docker_that_is_not_on_this_mac(fake):
    fake.docker_local = False
    with pytest.raises(stack.StackError, match="not this machine"):
        cluster.up()
    assert fake.actions() == ["docker context inspect"]


def expected_up(create: list[str]) -> list[str]:
    files = lambda names: " ".join(f"--filename {cluster.DEPLOY / name}" for name in names)  # noqa: E731
    out = ["docker context inspect", "kind get clusters", *create, "compose build fake-provider"]
    out += ["kind load docker-image klaxon-patient:dev --name klaxon"]
    out += [f"kubectl apply {files([cluster.NAMESPACE_FILE])}", "kubectl apply --filename - <stdin"]
    for names, waits in cluster.STAGES:
        out.append(f"kubectl apply {files(names)}")
        for workload in waits:
            if workload.startswith("job/"):
                out.append(f"kubectl wait --for=condition=complete {workload} --timeout=300s")
            else:
                out.append(f"kubectl rollout status {workload} --timeout=300s")
    return out


def test_up_creates_the_cluster_builds_and_loads_our_image_then_applies_in_order_and_waits(fake):
    cluster.up()
    create = f"kind create cluster --name klaxon --config {cluster.CONFIG} --kubeconfig {fake.kubeconfig} --wait 120s"
    assert fake.actions() == expected_up([create])
    # Each kubectl command came after its own check that the API server is on this Mac.
    calls = fake.kubectl_calls()
    for i, args in enumerate(calls):
        if args[6:8] != ["config", "view"]:
            assert i > 0 and calls[i - 1][6:8] == ["config", "view"], args
    (configmaps,) = [stdin for tool, _, stdin in fake.calls if stdin]
    assert json.loads(configmaps) == cluster.configmaps()


def test_up_on_a_cluster_that_is_there_creates_nothing(fake):
    fake.clusters = "klaxon\n"
    fake.kubeconfig.write_text("made up")
    cluster.up()
    assert fake.actions() == expected_up([])


def test_up_writes_this_clusters_kubeconfig_again_when_the_cluster_is_there_without_it(fake):
    fake.clusters = "other\nklaxon\n"
    cluster.up()
    assert fake.actions() == expected_up([f"kind export kubeconfig --name klaxon --kubeconfig {fake.kubeconfig}"])


def test_down_deletes_this_cluster_only(fake, capsys):
    fake.clusters = "other\nklaxon\n"
    cluster.down()
    assert fake.actions() == [
        "docker context inspect",
        "kind get clusters",
        f"kind delete cluster --name klaxon --kubeconfig {fake.kubeconfig}",
    ]
    fake.calls.clear()
    fake.clusters = "other\n"
    cluster.down()
    assert fake.actions() == ["docker context inspect", "kind get clusters"]
    assert "nothing to delete" in capsys.readouterr().out


def test_pods_reads_each_pods_status_and_never_its_spec(fake):
    fake.kubeconfig.write_text("made up")
    fake.pods = [made_up_pod("b", ready=False), made_up_pod("a", ready=True), made_up_pod("job", False, "Succeeded")]
    read = cluster.pods()
    assert [(p["name"], p["ready"], p["phase"]) for p in read] == [
        ("a", True, "Running"),
        ("b", False, "Running"),
        ("job", False, "Succeeded"),
    ]
    assert "made-up-password-not-real" not in json.dumps(read)


# The check on the cluster: refused unless the ports are the cluster's, and labeled as a run on kind


def made_up_run() -> under_traffic.UnderTrafficRun:
    """A traffic run that never ran, made up for the test."""

    def checked(n: int) -> under_traffic.CheckRun:
        return under_traffic.CheckRun(n, "2026-11-08T22:00:00.000+00:00", 1.0, "passed", [], None, None)

    return under_traffic.UnderTrafficRun(
        settings=traffic.Settings(rate=5.0, seed=7, run_id="madeup01"),
        warmup_seconds=5.0,
        checks=[checked(1), checked(2)],
        quiet=checked(0),
        traffic={"counts": {}, "unexpected": []},
        behavior_note="made up for a test",
    )


@pytest.fixture
def no_traffic(monkeypatch):
    def refused(**kwargs):
        raise AssertionError("traffic started")

    monkeypatch.setattr(under_traffic, "run_live", refused)


def test_the_check_refuses_while_the_compose_stack_holds_the_ports(fake, no_traffic):
    fake.kubeconfig.write_text("made up")
    fake.compose_up = [{"Service": "payments-api", "State": "running"}]
    with pytest.raises(cluster.ClusterError, match="Compose stack is up"):
        cluster.check(5.0, 2, 7, "made up")


@pytest.mark.parametrize("published", ["", "30080/tcp -> 0.0.0.0:8080\n", "30081/tcp -> 127.0.0.1:8080\n"])
def test_the_check_refuses_unless_the_node_publishes_each_port_to_its_node_port(fake, no_traffic, published):
    fake.kubeconfig.write_text("made up")
    fake.published = "".join(line for line in fake.published.splitlines(keepends=True) if ":8080" not in line)
    fake.published += published
    with pytest.raises(cluster.ClusterError, match="does not publish 127.0.0.1:8080 to 30080"):
        cluster.check(5.0, 2, 7, "made up")


def test_the_check_refuses_when_the_node_is_not_running(fake, no_traffic, monkeypatch):
    def gone(args, env=None, timeout=600):
        raise stack.StackError("docker port failed", stderr="Error: No such container: klaxon-control-plane")

    fake.kubeconfig.write_text("made up")
    monkeypatch.setattr(stack, "_run", gone)
    with pytest.raises(cluster.ClusterError, match="node is not running.*No such container"):
        cluster.check(5.0, 2, 7, "made up")


def test_the_check_refuses_while_a_pod_is_not_ready_but_takes_a_finished_job(fake, no_traffic):
    fake.kubeconfig.write_text("made up")
    fake.pods = [made_up_pod("kafka-init-x", False, "Succeeded"), made_up_pod("payments-api-y", ready=False)]
    with pytest.raises(cluster.ClusterError, match=r"not ready: payments-api-y \(make kind-ps\)"):
        cluster.check(5.0, 2, 7, "made up")


class AtOneTime(datetime):
    """Every now() the same made-up time: two runs in one second."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 8, 22, 0, 0, tzinfo=timezone.utc)


def test_the_check_saves_its_evidence_as_a_run_on_kind_never_over_another(fake, monkeypatch):
    fake.kubeconfig.write_text("made up")
    monkeypatch.setattr(cluster, "datetime", AtOneTime)
    monkeypatch.setattr(under_traffic, "run_live", lambda **kwargs: made_up_run())
    assert cluster.check(5.0, 2, 7, "python -m klaxon.patient.cluster check") == 0
    assert cluster.check(5.0, 2, 7, "python -m klaxon.patient.cluster check") == 0
    paths = sorted((fake.kubeconfig.parent / "evidence").iterdir())
    assert [p.name for p in paths] == [
        "2026-11-08T220000Z_kind-check-under-traffic_rate5_seed7-2.json",
        "2026-11-08T220000Z_kind-check-under-traffic_rate5_seed7.json",
    ]
    saved = json.loads(paths[0].read_text())
    assert saved["label"].startswith("Real run on this Mac's kind cluster") and "Compose stack," not in saved["label"]
    assert saved["printed_output"][1].startswith("REAL RUN on this Mac's kind cluster (Kubernetes in Docker")
    assert saved["scenario"] == "kind-check-under-traffic" and saved["every_check_passed"] is True
    assert saved["cluster"]["kind"] == "kind v0.0.0-made-up" and saved["cluster"]["node_image"].startswith("kindest")
    assert saved["pods_after"][0]["name"] == "payments-api-5d9c7-x2v4q"
    assert "made-up-password-not-real" not in paths[0].read_text()


def test_the_compose_runs_printout_is_as_it_was():
    printed = under_traffic.render(made_up_run()).splitlines()
    assert printed[0] == "Klaxon week 2: the money check, run while synthetic traffic flows"
    assert printed[1].startswith("REAL RUN on this Mac's Docker Compose stack. Traffic run madeup01, seed 7:")


def test_the_make_targets_drive_this_module():
    makefile = (cluster.REPO / "Makefile").read_text()
    for command in ("up", "down", "ps"):
        assert f"\t$(PY) -m klaxon.patient.cluster {command}\n" in makefile
    assert "\t$(PY) -m klaxon.patient.cluster check --checks $(or $(CHECKS),20) --rate $(or $(RATE),5)\n" in makefile
    assert ".kind/" in (cluster.REPO / ".gitignore").read_text().splitlines()
