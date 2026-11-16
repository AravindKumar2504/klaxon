"""Klaxon undoes a bad rollout on the kind cluster (week 6, item 4), offline.

Its second account and what Kubernetes lets it change; its token; the rollback planned from kind's deploy log and run
through that account only, with the gate unchanged; verification's health on kind; and a gated run's wiring on kind.
Against item 3's made-up kubectl and cluster (tests.test_cluster): not a cluster. Kubernetes' own refusals (its RBAC)
cannot be checked offline: they are checked live, with Aravind's OK.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from klaxon import fault_runs
from klaxon.agent import actions, gate, verify
from klaxon.agent.contract import FixAction
from klaxon.agent.tools import PLATFORM_VARIABLE
from klaxon.patient import cluster, deploys
from tests.agent.test_gate import act, events, made_up_diagnosis
from tests.agent.test_verify import Clock, rounds_of
from tests.test_alerts import Readings
from tests.test_cluster import DOCS, GOOD_API, OBJECTS, SERVER, Fake, MadeUpCluster
from tests.test_fault_runs import World, patch_command

REPO = cluster.REPO
DOLLARS = GOOD_API | {"CHARGE_AMOUNT_UNIT": "dollars"}  # fault 4, the money-unit bug
ROLLBACK_API = FixAction("rollback_deploy", {"service": "payments-api"})


# The second account: what Kubernetes lets Klaxon's rollback change


ROLLBACK = {doc["kind"]: doc for doc in DOCS[cluster.ROLLBACK_FILE]}


def test_klaxons_rollback_may_only_get_watch_and_patch_our_four_deployments_and_read_replica_sets():
    role = ROLLBACK["Role"]
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": sorted(deploys.SERVICES),  # our four, by name: never redis, never another
            "verbs": ["get", "list", "watch", "patch"],
        },
        {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["get", "list"]},
    ]
    writes = {verb for rule in role["rules"] for verb in rule["verbs"]} - {"get", "list", "watch"}
    assert writes == {"patch"}  # no create, no update, no delete, no deletecollection, no *
    allowed = {resource for rule in role["rules"] for resource in rule["resources"]}
    assert not allowed & {"pods", "secrets", "configmaps", "statefulsets", "serviceaccounts", "*"}
    binding = ROLLBACK["RoleBinding"]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": cluster.ROLLBACK}
    subject = {"kind": "ServiceAccount", "name": cluster.ROLLBACK, "namespace": cluster.NAMESPACE}
    assert binding["subjects"] == [subject]
    assert ROLLBACK["ServiceAccount"]["automountServiceAccountToken"] is False  # no pod ever runs with it
    names = {doc["metadata"]["name"] for doc in ROLLBACK.values()}
    assert names == {cluster.ROLLBACK} and {doc["metadata"]["namespace"] for doc in ROLLBACK.values()} == {"klaxon"}
    assert not [o for o in OBJECTS if o["kind"] in ("ClusterRole", "ClusterRoleBinding")]  # nothing cluster-wide


def test_the_tools_account_is_not_the_rollbacks_and_neither_is_bound_to_the_others_role():
    reader = {doc["kind"]: doc for doc in DOCS[cluster.ACCESS_FILE]}
    assert cluster.READER != cluster.ROLLBACK
    assert reader["RoleBinding"]["subjects"][0]["name"] == cluster.READER
    assert ROLLBACK["RoleBinding"]["roleRef"]["name"] == cluster.ROLLBACK != reader["Role"]["metadata"]["name"]


class RollbackFake(Fake):
    """Item 3's made-up kind and kubectl, which also gives a made-up token of klaxon-rollback. Not a cluster."""

    rollback_token = "made-up-rollback-token-not-real"

    def run(self, binary, args, *, stdin=None, timeout=600):
        if Path(binary).name == "kubectl" and args[8:11] == ["create", "token", cluster.ROLLBACK]:
            self.calls.append(("kubectl", list(args), stdin))
            return self.rollback_token + "\n"
        if Path(binary).name == "kubectl" and args[8:10] == ["get", cluster.HEALTH_KINDS]:
            self.calls.append(("kubectl", list(args), stdin))
            return json.dumps({"kind": "List", "items": self.pods})
        return super().run(binary, args, stdin=stdin, timeout=timeout)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    for name in ("KUBECONFIG", "KIND_EXPERIMENTAL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    made_up = RollbackFake(tmp_path, monkeypatch)
    made_up.kubeconfig.write_text("made up")
    monkeypatch.setattr(cluster, "READER_KUBECONFIG", tmp_path / "reader.kubeconfig")
    monkeypatch.setattr(cluster, "ROLLBACK_KUBECONFIG", tmp_path / "rollback.kubeconfig")
    return made_up


def test_the_rollbacks_kubeconfig_holds_a_token_of_its_own_readable_by_this_user_only(fake, tmp_path):
    path = cluster.make_rollback_kubeconfig()
    assert path == tmp_path / "rollback.kubeconfig" and path.stat().st_mode & 0o777 == 0o600
    config = json.loads(path.read_text())
    assert config["users"] == [{"name": cluster.ROLLBACK, "user": {"token": fake.rollback_token}}]
    assert config["clusters"][0]["cluster"]["server"] == SERVER
    assert config["current-context"] == cluster.ROLLBACK_CONTEXT == "kind-klaxon-rollback"
    assert config["contexts"][0]["context"] == {
        "cluster": cluster.CONTEXT,
        "user": cluster.ROLLBACK,
        "namespace": "klaxon",
    }
    asked = [args[8:] for args in fake.kubectl_calls()]
    assert ["create", "token", cluster.ROLLBACK, "--duration=3600s"] in asked  # an hour
    assert not (tmp_path / "reader.kubeconfig").exists()  # the reader's stays its own: made only when asked for


def test_the_rollbacks_token_is_never_the_readers_and_each_file_names_its_own_account(fake, tmp_path):
    reader, rollback = cluster.make_reader_kubeconfig(), cluster.make_rollback_kubeconfig()
    tokens = [json.loads(path.read_text())["users"][0]["user"]["token"] for path in (reader, rollback)]
    assert tokens == [fake.token, fake.rollback_token] and reader != rollback


def test_the_rollbacks_kubectl_names_its_own_file_and_context_and_refuses_without_it(fake, tmp_path):
    with pytest.raises(cluster.ClusterError, match="missing"):
        cluster.rollback_kubectl("get", "deployment", "payments-api")
    (tmp_path / "rollback.kubeconfig").write_text("made up")
    cluster.rollback_kubectl("patch", "deployment", "payments-api", "--type", "json", "--patch", "[]")
    *_, check, call = fake.kubectl_calls()
    for args in (check, call):
        assert args[:6] == [
            "--kubeconfig",
            str(tmp_path / "rollback.kubeconfig"),
            "--context",
            cluster.ROLLBACK_CONTEXT,
            "--namespace",
            "klaxon",
        ]
    assert call[8:] == ["patch", "deployment", "payments-api", "--type", "json", "--patch", "[]"]


def test_the_rollbacks_kubectl_refuses_a_cluster_whose_api_server_is_not_on_this_mac(fake, tmp_path):
    (tmp_path / "rollback.kubeconfig").write_text("made up")
    fake.server = "https://10.0.0.9:6443"
    with pytest.raises(cluster.ClusterError, match="not on this Mac"):
        cluster.rollback_kubectl("get", "deployment", "payments-api")
    assert [args[8:10] for args in fake.kubectl_calls()] == [["config", "view"]]  # checked, never asked


# (make kind-up applies the rollback's account with the tools' one: tests.test_cluster's test of up() checks it.)


# Klaxon's deployer on kind: kind's own log, klaxon-rollback's access


class Account:
    """One account's kubectl on one made-up cluster: each call kept with the account's name. Made up."""

    def __init__(self, made_up: MadeUpCluster, name: str, calls: list) -> None:
        self.made_up, self.name, self.calls = made_up, name, calls

    def __call__(self, *args: str, stdin: str | None = None, timeout: float = 600) -> str:
        self.calls.append((self.name, args))
        return self.made_up(*args, timeout=timeout)


def kind_pair(tmp_path, made_up=None):
    """The harness's deployer (the admin's kubectl) and Klaxon's (klaxon-rollback's), on one made-up cluster and one
    kind deploy log, as a gated run on kind has them."""
    made_up = made_up or MadeUpCluster()
    calls: list = []
    path = tmp_path / "deploys-kind.jsonl"
    admin = cluster.ClusterDeployer(
        deploys.DeployLog(path), kubectl=Account(made_up, "admin", calls), ensure_local=lambda: None
    )
    klaxons = cluster.ClusterDeployer(
        deploys.DeployLog(path), kubectl=Account(made_up, cluster.ROLLBACK, calls), ensure_local=lambda: None
    )
    return made_up, calls, admin, klaxons


def test_klaxons_deployer_on_kind_is_kinds_own_log_through_klaxon_rollback(monkeypatch, tmp_path):
    monkeypatch.setenv("KLAXON_KIND_DEPLOY_LOG", str(tmp_path / "kind.jsonl"))
    klaxons = cluster.klaxons_deployer()
    assert klaxons._kubectl is cluster.rollback_kubectl and klaxons.log.path == tmp_path / "kind.jsonl"
    assert cluster.ClusterDeployer().log.path == klaxons.log.path  # the faults' log too: one log, one lock
    assert cluster.ClusterDeployer()._kubectl is cluster.kubectl  # the faults keep the admin's


def test_an_approved_rollback_on_kind_puts_back_the_settings_before_the_bad_deploy_through_klaxon_rollback(tmp_path):
    made_up, calls, admin, klaxons = kind_pair(tmp_path)
    admin.deploy("payments-api", DOLLARS, by="klaxon.faults")  # fault 4, as the harness rolls it out
    faulted = len(calls)
    planned = actions.plan("rollback_deploy", {"service": "payments-api"}, klaxons)
    assert (planned.from_settings, planned.to_settings) == (DOLLARS, GOOD_API)
    assert planned.container == cluster.hashlib.sha256(b"rs-uid-2").hexdigest()  # the bad deploy's revision
    done = actions.run(planned, klaxons, by="Klaxon, approved request r-made-up")
    assert done.settings == GOOD_API and done.replaced_settings == DOLLARS
    assert {name for name, _ in calls[faulted:]} == {cluster.ROLLBACK}  # from the plan on: never the admin's access
    (patch,) = [args for name, args in calls[faulted:] if args[0] == "patch"]
    steps = json.loads(patch[patch.index("--patch") + 1])
    assert steps[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "101"}  # unchanged since read
    assert ("rollout", "status", "deployment/payments-api", "--timeout=300s") in [args for _, args in calls]
    assert {item["name"]: item.get("value") for item in made_up.env if "value" in item} == GOOD_API
    log = deploys.DeployLog(tmp_path / "deploys-kind.jsonl").records()
    assert [line.by for line in log] == ["klaxon.faults", "Klaxon, approved request r-made-up"]


def test_a_rollback_on_kind_planned_before_another_deploy_is_refused_and_patches_nothing(tmp_path):
    made_up, calls, admin, klaxons = kind_pair(tmp_path)
    admin.deploy("payments-api", DOLLARS, by="klaxon.faults")
    planned = actions.plan("rollback_deploy", {"service": "payments-api"}, klaxons)
    admin.deploy("payments-api", DOLLARS | {"REFUND_CONNECTION": "kept"}, by="someone else")  # while it waited
    before = len(calls)
    with pytest.raises(actions.ActionRefused, match="changed since the request was made: nothing was done"):
        actions.run(planned, klaxons, by="Klaxon, approved request r-made-up")
    assert not [args for _, args in calls[before:] if args[0] == "patch"]


def test_a_rollback_on_kind_the_cluster_refuses_because_the_deployment_moved_changes_nothing(tmp_path):
    made_up, calls, admin, klaxons = kind_pair(tmp_path)
    admin.deploy("payments-api", DOLLARS, by="klaxon.faults")
    planned = actions.plan("rollback_deploy", {"service": "payments-api"}, klaxons)
    made_up.refuse = "moved"  # Kubernetes refuses the whole patch: its resourceVersion test failed
    with pytest.raises(actions.ActionRefused, match="changed since the request was made"):
        actions.run(planned, klaxons, by="Klaxon, approved request r-made-up")
    assert [line.by for line in deploys.DeployLog(tmp_path / "deploys-kind.jsonl").records()] == ["klaxon.faults"]


def test_undoing_klaxons_rollback_on_kind_is_refused_as_on_compose(tmp_path):
    made_up, calls, admin, klaxons = kind_pair(tmp_path)
    admin.deploy("payments-api", DOLLARS, by="klaxon.faults")
    planned = actions.plan("rollback_deploy", {"service": "payments-api"}, klaxons)
    done = actions.run(planned, klaxons, by="Klaxon, approved request r-made-up")
    with pytest.raises(actions.ActionRefused, match="known-bad setting"):
        actions.plan_undo({"service": done.service, "seq": done.seq}, klaxons)


def test_the_gate_on_kind_asks_waits_for_the_typed_approval_and_rolls_back_through_klaxon_rollback(tmp_path):
    """The gate unchanged (klaxon.agent.gate), with kind's deployer: the request, the typed approval in make approve's
    real code (a scripted terminal stands in for his), the rollback, the log."""
    made_up, calls, admin, klaxons = kind_pair(tmp_path)
    admin.deploy("payments-api", DOLLARS, by="klaxon.faults")
    diagnosis = made_up_diagnosis(fix_action=ROLLBACK_API)
    (tmp_path / "run").mkdir()
    outcome = act(tmp_path / "run", None, klaxons, diagnosis=diagnosis)
    request = gate.read_request(tmp_path / "run")
    assert (outcome.result, outcome.request_id) == ("done", request["id"])
    assert events(tmp_path / "run") == ["requested", "approved", "decision taken", "running", "done"]
    latest = deploys.DeployLog(tmp_path / "deploys-kind.jsonl").latest("payments-api")
    assert latest.by == f"Klaxon, approved request {request['id']}" and latest.settings == GOOD_API
    assert [name for name, args in calls if args[0] == "patch"] == ["admin", cluster.ROLLBACK]


# Verification's health on kind: every rollout done, every pod ready


def deployment(name, generation=2, observed=2, updated=1, replicas=1, available=1):
    """A Deployment of one replica as `kubectl get --output json` describes one: replicas counts its pods, old and
    new; updated, those of its newest template; available, those ready for long enough. Made up."""
    status = {"observedGeneration": observed, "updatedReplicas": updated, "replicas": replicas}
    status |= {"availableReplicas": available, "readyReplicas": available}
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "generation": generation},
        "spec": {"replicas": 1},
        "status": status,
    }


def statefulset(name, ready=1, current="a", update="a"):
    status = {"readyReplicas": ready, "currentRevision": current, "updateRevision": update}
    return {"kind": "StatefulSet", "metadata": {"name": name}, "spec": {"replicas": 1}, "status": status}


def pod(name, ready=True, phase="Running", leaving=False):
    meta = {"name": name} | ({"deletionTimestamp": "2026-11-13T00:00:00Z"} if leaving else {})
    return {"kind": "Pod", "metadata": meta, "status": {"phase": phase, "containerStatuses": [{"ready": ready}]}}


HEALTHY = [
    deployment("payments-api"),
    deployment("ledger-consumer"),
    statefulset("postgres"),
    pod("payments-api-5d9c7-x2v4q"),
    pod("ledger-consumer-6b8d4-k3j2p"),
    pod("postgres-0"),
    pod("kafka-init-9x7z2", ready=False, phase="Succeeded"),  # the topics Job's pod: done
    pod("payments-api-4c1a2-old11", ready=False, leaving=True),  # a rollout's old pod, on its way out
]


def test_a_cluster_with_every_rollout_done_and_every_pod_ready_is_healthy():
    assert verify.cluster_unwell(HEALTHY) is None


@pytest.mark.parametrize(
    "changed, why",
    [
        # Each of the four things kubectl rollout status waits for, broken alone.
        (deployment("payments-api", observed=1), "payments-api rolling out"),  # its newest generation not seen yet
        (deployment("payments-api", updated=0, replicas=0, available=0), "payments-api rolling out"),  # none updated
        (deployment("payments-api", replicas=2), "payments-api rolling out"),  # an old pod still counted
        (deployment("payments-api", available=0), "payments-api rolling out"),  # its new pod not available yet
        (statefulset("postgres", ready=0), "postgres not ready"),
        (statefulset("postgres", update="b"), "postgres not ready"),
        (pod("payments-api-5d9c7-x2v4q", ready=False), "payments-api-5d9c7-x2v4q Running"),
        (pod("ledger-consumer-6b8d4-k3j2p", ready=False, phase="Pending"), "ledger-consumer-6b8d4-k3j2p Pending"),
    ],
)
def test_a_rollout_not_done_or_a_pod_not_ready_is_named(changed, why):
    objects = [item for item in HEALTHY if item["metadata"]["name"] != changed["metadata"]["name"]] + [changed]
    assert verify.cluster_unwell(objects) == f"not healthy: {why}"


def test_a_pod_with_no_container_status_yet_is_not_ready():
    starting = {"kind": "Pod", "metadata": {"name": "redis-0"}, "status": {"phase": "Pending"}}
    assert verify.cluster_unwell([*HEALTHY, starting]) == "not healthy: redis-0 Pending"


def test_a_cluster_that_lists_no_pod_is_not_healthy():
    assert verify.cluster_unwell([deployment("payments-api")]) == "the cluster lists no pod of the stack"
    assert verify.cluster_unwell([]) == "the cluster lists no pod of the stack"


def test_health_on_kind_reads_as_klaxon_reader_the_workloads_and_pods(fake, tmp_path):
    (tmp_path / "reader.kubeconfig").write_text("made up")
    fake.pods = [pod("payments-api-5d9c7-x2v4q")]
    assert cluster.health_objects() == fake.pods
    *_, call = fake.kubectl_calls()
    assert call[:4] == ["--kubeconfig", str(tmp_path / "reader.kubeconfig"), "--context", cluster.READER_CONTEXT]
    assert (
        call[8:] == ["get", cluster.HEALTH_KINDS, "--output", "json"]
        and cluster.HEALTH_KINDS == "deployments,statefulsets,pods"
    )


def test_health_on_kind_refuses_an_answer_of_another_shape(monkeypatch):
    monkeypatch.setattr(cluster, "reader_kubectl", lambda *args, **kw: json.dumps({"kind": "List"}))
    with pytest.raises(cluster.ClusterError, match="unexpected shape"):
        cluster.health_objects()


def test_a_cluster_that_cannot_be_read_is_not_healthy_and_says_it_was_the_cluster():
    clock = Clock()

    def unreachable():
        raise cluster.ClusterError("made up")

    health = verify.wait_healthy(
        rounds_of(Readings()),
        unreachable,
        clock.sleep,
        clock,
        within=30.0,
        unwell=verify.cluster_unwell,
        what="the cluster",
    )
    assert not health.healthy and health.why == "the cluster could not be read (ClusterError)"
    sick = [*HEALTHY, pod("payments-api-5d9c7-x2v4q", ready=False)]
    health = verify.wait_healthy(
        rounds_of(Readings()), lambda: sick, clock.sleep, clock, within=30.0, unwell=verify.cluster_unwell
    )
    assert not health.healthy and health.why == "not healthy: payments-api-5d9c7-x2v4q Running"
    healthy = verify.wait_healthy(
        rounds_of(Readings()), lambda: HEALTHY, clock.sleep, clock, unwell=verify.cluster_unwell
    )
    assert healthy.healthy and len(healthy.rounds) == verify.QUIET_IN_A_ROW


def test_verify_gives_health_the_stacks_own_reading_and_judge(monkeypatch):
    seen = []

    def wait_healthy(rules, containers, sleep, clock, within, every, unwell, what):
        seen.append((containers, unwell, what))
        return verify.Health(True, None, ())

    monkeypatch.setattr(verify, "wait_healthy", wait_healthy)
    monkeypatch.setattr(verify, "read", lambda check, settle, now: verify.Reading("2026-11-13T00:00:00Z", "passed", 0))
    verify.verify(lambda: None, containers=cluster.health_objects, sleep=lambda s: None, observe=0,
                  unwell=verify.cluster_unwell, what="the cluster")  # fmt: skip
    verify.verify(lambda: None, sleep=lambda s: None, observe=0)  # Compose's, by default
    assert seen == [
        (cluster.health_objects, verify.cluster_unwell, "the cluster"),
        (verify.read_containers, verify.containers_unwell, "docker"),
    ]


# A gated run on kind: its parts, and how the command puts them together


def test_kind_parts_roll_back_through_klaxons_deployer_and_verify_the_clusters_health(monkeypatch, tmp_path):
    seen = {}

    class Deployer:
        log = deploys.DeployLog(tmp_path / "kind.jsonl")

    monkeypatch.setattr(fault_runs.cluster, "klaxons_deployer", lambda: Deployer)
    monkeypatch.setattr(
        fault_runs.gate, "act", lambda folder, diagnosis, deployer, alert, **kw: seen.update(act=deployer)
    )
    monkeypatch.setattr(
        fault_runs.gate, "propose_undo", lambda folder, outcome, deployer, why: seen.update(undo=deployer)
    )
    monkeypatch.setattr(fault_runs.verify, "verify", lambda rules, **kw: seen.update(verify=kw))
    parts = fault_runs.kind_parts(tmp_path)
    alert = fault_runs.Alert("MoneyCheckFailed", "made up")
    parts.act(alert, made_up_diagnosis(fix_action=ROLLBACK_API), lambda: None)
    parts.verify_action(lambda: None)

    class Failed:  # a failed verification, as far as propose_undo reads one: made up
        said = ("made up",)

    parts.propose_undo(gate.Outcome("done", "made up", "r-made-up"), Failed())
    assert seen["act"] is Deployer and seen["undo"] is Deployer
    assert seen["verify"] == {
        "containers": cluster.health_objects,
        "unwell": verify.cluster_unwell,
        "what": "the cluster",
    }
    assert parts.deploys == Deployer.log.records


def test_klaxons_tools_on_kind_start_with_the_platform_set_and_the_rest_of_the_environment(monkeypatch):
    monkeypatch.setattr(fault_runs, "McpClient", lambda env: env)  # what the tool server would start with
    env = fault_runs.kind_tools()
    assert env[PLATFORM_VARIABLE] == "kind" and env == os.environ | {PLATFORM_VARIABLE: "kind"}


def made_up_kind_run(monkeypatch, tmp_path):
    """A fault run on kind with each of its parts made up, noting what it made (made) and what it was given (given).
    It stops at the loop: KeyboardInterrupt, so fault_runs.main returns 130."""
    world = World()
    patch_command(monkeypatch, world)
    made, given = [], {}
    monkeypatch.setattr(fault_runs.cluster, "ready", lambda: made.append("ready"))
    monkeypatch.setattr(fault_runs.cluster, "make_reader_kubeconfig", lambda: made.append("reader's token"))
    monkeypatch.setattr(fault_runs.cluster, "make_rollback_kubeconfig", lambda: made.append("rollback's token"))
    monkeypatch.setattr(fault_runs, "kind_tools", lambda: "kind's tool server, made up")
    monkeypatch.setattr(fault_runs, "preflight", lambda model, client: made.append(("preflight", client())))
    monkeypatch.setattr(fault_runs.faults, "LocalStack", lambda **kw: made.append(kw))
    monkeypatch.setattr(fault_runs.cluster, "ClusterDeployer", lambda: "the faults' deployer on kind, made up")
    monkeypatch.setattr(fault_runs.cluster, "describe", lambda: {"kubernetes_server": "v0-made-up"})
    monkeypatch.setattr(
        fault_runs.cluster,
        "provenance",
        lambda command, now, described: {"rerun_command": command, "cluster": described},
    )
    monkeypatch.setattr(fault_runs, "_provenance", lambda command: pytest.fail("Compose's provenance, on kind"))
    monkeypatch.setattr(fault_runs, "klaxon_parts", lambda *a: pytest.fail("Compose's gate parts, on kind"))
    monkeypatch.setattr(fault_runs, "kind_parts", lambda folder: given.setdefault("parts", ("kind's parts", folder)))
    monkeypatch.setattr(fault_runs, "KIND_EVIDENCE", tmp_path / "kind")

    def run(fault, record, parts, settings, investigate, gated=None):
        given["gated"], given["platform"] = gated, record.platform
        investigate(fault_runs.Alert("PaymentsErrorsHigh", "made up"))

    monkeypatch.setattr(fault_runs, "run", run)

    def investigate_live(
        alert, budgets, folder, header, model_id, binary, actions=(), tool_client=None, loop_kind=fault_runs.loop.PLAIN
    ):
        given["tool_client"], given["actions"], given["loop_kind"] = tool_client, actions, loop_kind
        raise KeyboardInterrupt("made up: stop here")

    monkeypatch.setattr(fault_runs.loop, "investigate_live", investigate_live)
    return made, given


def test_a_gated_run_on_kind_makes_both_tokens_starts_kinds_tools_and_acts_with_kinds_parts(monkeypatch, tmp_path):
    made, given = made_up_kind_run(monkeypatch, tmp_path)
    assert fault_runs.main(["run", "money-unit", "--platform", "kind"]) == 130
    # Checked ready, then the two tokens, then the tools listed on kind, then the faults' own deployer on kind.
    assert made == [
        "ready",
        "reader's token",
        "rollback's token",
        ("preflight", "kind's tool server, made up"),
        {"deployer": "the faults' deployer on kind, made up"},
    ]
    [folder] = (tmp_path / "kind").iterdir()  # its own folder: evidence/week6/kind
    assert folder.name.endswith("_gated-run_money-unit")
    assert given["gated"] == ("kind's parts", folder) and given["platform"] == "kind"
    assert given["tool_client"] == "kind's tool server, made up" and [a.name for a in given["actions"]] == [
        "rollback_deploy"
    ]
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["platform"] == "kind" and saved["label"].startswith("Real run on this Mac's kind cluster (Kubernetes")
    assert saved["cluster"] == {"kubernetes_server": "v0-made-up"} and "--platform kind" in saved["rerun_command"]
    # Week 6, items 2 and 4 together: a run on kind takes the plain loop unless told, and its files say so.
    assert given["loop_kind"] == fault_runs.loop.PLAIN and saved["loop_kind"] == fault_runs.loop.PLAIN


def test_a_diagnosis_only_run_on_kind_gets_no_write_access_and_saves_its_command_without_the_home_folder(
    monkeypatch, tmp_path
):
    # Week 6, item 5 (the review's finding 11): a run that offers no action needs no token of klaxon-rollback, and its
    # saved command writes the home folder as ~, as Compose's _provenance does.
    import klaxon.agent.tools as agent_tools

    made, given = made_up_kind_run(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_tools, "HOME", str(tmp_path))  # the home folder, made up: clean_text writes it as ~
    argv = ["run", "money-unit", "--platform", "kind", "--no-actions", "--evidence-dir", str(tmp_path / "kind")]
    assert fault_runs.main(argv) == 130
    assert made[:2] == ["ready", "reader's token"] and "rollback's token" not in made
    assert given["actions"] == () and "parts" not in given  # nothing offered, so no gate parts either
    [folder] = (tmp_path / "kind").iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert str(tmp_path) not in saved["rerun_command"] and "--evidence-dir ~/kind" in saved["rerun_command"]


def test_a_gated_run_on_kind_touches_nothing_when_the_cluster_is_not_ready(monkeypatch, tmp_path, capsys):
    world = World()
    patch_command(monkeypatch, world)

    def not_ready():
        raise fault_runs.cluster.ClusterError("not ready: payments-api-5d9c7-x2v4q (make kind-ps)")

    monkeypatch.setattr(fault_runs.cluster, "ready", not_ready)
    monkeypatch.setattr(fault_runs.cluster, "make_rollback_kubeconfig", lambda: pytest.fail("a token was made"))
    monkeypatch.setattr(fault_runs.faults, "LocalStack", lambda **kw: pytest.fail("nothing is made"))
    argv = ["run", "money-unit", "--platform", "kind", "--evidence-dir", str(tmp_path)]
    assert fault_runs.main(argv) == 2
    assert list(tmp_path.iterdir()) == [] and "not ready: payments-api" in capsys.readouterr().err


def test_a_gated_run_on_compose_keeps_week_4s_folder_its_parts_and_its_tool_server(monkeypatch, tmp_path):
    world = World()
    given = patch_command(monkeypatch, world)
    monkeypatch.setattr(fault_runs, "REPO", tmp_path)  # week 4's folder, under a made-up repo
    monkeypatch.setattr(fault_runs, "kind_parts", lambda folder: pytest.fail("kind's parts, on Compose"))
    assert fault_runs.main(["run", "provider-outage", "--no-actions"]) == 0
    [folder] = (tmp_path / "evidence" / "week4").iterdir()
    assert json.loads((folder / fault_runs.RUN_FILE).read_text())["platform"] == "compose"
    assert given  # the loop ran, with Compose's tool server (its stand-in checks tool_client is None)


# The postmortem's words on kind, and make investigate-fault PLATFORM=kind


FIRST_GATED = REPO / "evidence" / "week4" / "2026-10-21T090513Z_gated-run_duplicate-delivery"  # verified, on Compose


def test_the_postmortems_health_line_names_what_health_read_where_the_run_ran():
    from klaxon.agent import postmortem

    found = json.loads((FIRST_GATED / fault_runs.RUN_FILE).read_text())["verification"]
    words = {
        platform: [row["what"] for row in postmortem._verification_rows(found, None, platform)]
        for platform in ("compose", "kind")
    }
    compose = [what for what in words["compose"] if what.startswith("Health, by code:")]
    kind = [what for what in words["kind"] if what.startswith("Health, by code:")]
    assert len(compose) == len(kind) == 1 and "of the rules and the containers, from" in compose[0]
    assert kind[0] == compose[0].replace("the containers", "the cluster's rollouts and pods")
    # A run saved before runs said where they ran is Compose's, and its facts keep their words.
    assert postmortem.HEALTH_READ["compose"] == "the containers"


def test_a_postmortem_of_a_run_on_kind_says_what_health_read_there(tmp_path):
    from klaxon.agent import postmortem

    def health_line(folder):
        rows = postmortem.facts(folder)["timeline"]
        return [row["what"] for row in rows if row["what"].startswith("Health, by code:")]

    copy = tmp_path / FIRST_GATED.name
    shutil.copytree(FIRST_GATED, copy)
    saved = json.loads((copy / fault_runs.RUN_FILE).read_text())
    (copy / fault_runs.RUN_FILE).write_text(json.dumps(saved | {"platform": "kind"}))  # made up: as if on kind
    [compose], [kind] = health_line(FIRST_GATED), health_line(copy)
    assert "the containers" in compose and kind == compose.replace("the containers", "the cluster's rollouts and pods")


def test_make_investigate_fault_passes_platform_on_as_one_argument_and_only_when_given():
    env = {key: value for key, value in os.environ.items() if key != "PLATFORM"}

    def made(*more):
        done = subprocess.run(
            ["make", "--no-print-directory", "-s", "investigate-fault", "FAULT=money-unit", *more, "PY=printf '%s\\n'"],
            cwd=REPO, capture_output=True, text=True, env=env, check=True, timeout=60,
        )  # fmt: skip
        return done.stdout.splitlines()

    assert "--platform" not in made()
    given = made("PLATFORM=kind")
    assert given[given.index("--platform") + 1] == "kind"
    odd = made("PLATFORM=kind'; echo pwned")
    assert odd[odd.index("--platform") + 1] == "kind'; echo pwned" and "pwned" not in odd


def test_the_gated_command_takes_only_the_platforms_this_code_knows(capsys):
    with pytest.raises(SystemExit):
        fault_runs.main(["run", "money-unit", "--platform", "k8s"])
    assert "invalid choice: 'k8s'" in capsys.readouterr().err
