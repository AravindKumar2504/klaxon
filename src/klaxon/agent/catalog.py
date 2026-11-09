"""The write actions a diagnosis may name: their names, what they do, and their arguments. Data only.

Klaxon's model never runs an action. When a run offers actions, its diagnosis may name one (fix_action, in
klaxon.agent.contract). Code then plans it from the live state (klaxon.agent.actions), a person approves or denies
it (klaxon.agent.gate), and only an approved plan runs. This module holds no code that acts, so the loop can show
the catalog to the model without being able to use it.

One action so far (Aravind's choice, 2026-10-19): rollback_deploy. Money repairs, such as reversing double
postings or refunding an overcharge, stay proposals for a person to carry out. A run records the actions it offered,
as it offered them, and a replay offers those (klaxon.agent.loop), so a change here never changes a recorded run.
"""

from __future__ import annotations

from klaxon.agent.contract import ActionSpec

# Services whose settings the deploy log knows (klaxon.patient.deploys.SERVICES; a test checks it). The payments API
# and the webhook receiver since week 4, item 4: faults 4 to 6 are bad deploys of theirs (Aravind's choice, 2026-10-23).
ROLLBACK_SERVICES = ("ledger-consumer", "payment-confirmer", "payments-api", "webhook-receiver")
# The second sentence since week 6 (Aravind's call at his review of week 5, 2026-11-09; Claude's draft, the words he
# chose on 2026-11-09): in week 5, 5 of the 6 runs that could not read deploys named no rollback. Each clause is what
# klaxon.agent.actions.plan() does: it reads the deploy log and the running service, never the model's tool results.
# Each run records the actions it offered, whole, so a saved run replays with the words it had (klaxon.agent.loop).
ROLLBACK = (
    "Roll one service back to the settings it ran before its latest deploy, as the deploy log records them. Code"
    " plans it from that log, not from your tools' results, so you can name it even when they cannot see deploys: if"
    " the log holds no deploy of that service to undo, or the service changed since, code refuses it. Its container"
    " is recreated, so the service pauses while it restarts. Money already booked wrong stays wrong: a rollback stops"
    " new damage, and repairs nothing."
)

ROLLBACK_DEPLOY = ActionSpec(
    name="rollback_deploy",
    description=ROLLBACK,
    input_schema={
        "type": "object",
        "properties": {"service": {"enum": list(ROLLBACK_SERVICES), "description": "The service to roll back."}},
        "required": ["service"],
        "additionalProperties": False,
    },
)

CATALOG: dict[str, ActionSpec] = {spec.name: spec for spec in (ROLLBACK_DEPLOY,)}

# Each action as the runs offered it before a run's trace recorded the actions it offered (week 4, item 4): the three
# gated runs of item 2 recorded only its name, and offered it for the two consumers. A replay of such a run offers it
# as it was then, so the run's system prompt is the same, byte for byte (a test checks it against those runs). Written
# out, not built like today's: a change to today's action must never change it.
FIRST_OFFERED: dict[str, ActionSpec] = {
    "rollback_deploy": ActionSpec(
        name="rollback_deploy",
        description=(
            "Roll one service back to the settings it ran before its latest deploy, as the deploy log records them."
            " Its container is recreated, so the service pauses while it restarts. Money already booked wrong stays"
            " wrong: a rollback stops new damage, and repairs nothing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "service": {
                    "enum": ["ledger-consumer", "payment-confirmer"],
                    "description": "The service to roll back.",
                }
            },
            "required": ["service"],
            "additionalProperties": False,
        },
    )
}
