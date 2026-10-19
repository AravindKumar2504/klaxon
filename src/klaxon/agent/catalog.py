"""The write actions a diagnosis may name: their names, what they do, and their arguments. Data only.

Klaxon's model never runs an action. When a run offers actions, its diagnosis may name one (fix_action, in
klaxon.agent.contract). Code then plans it from the live state (klaxon.agent.actions), a person approves or denies
it (klaxon.agent.gate), and only an approved plan runs. This module holds no code that acts, so the loop can show
the catalog to the model without being able to use it.

One action so far (Aravind's choice, 2026-10-19): rollback_deploy. Money repairs, such as reversing double
postings or refunding an overcharge, stay proposals for a person to carry out.
"""

from __future__ import annotations

from klaxon.agent.contract import ActionSpec

# The services whose settings the deploy log knows (klaxon.patient.deploys.SERVICES; a test checks they agree).
ROLLBACK_SERVICES = ("ledger-consumer", "payment-confirmer")

ROLLBACK_DEPLOY = ActionSpec(
    name="rollback_deploy",
    description=(
        "Roll one service back to the settings it ran before its latest deploy, as the deploy log records them."
        " Its container is recreated, so the service pauses while it restarts. Money already booked wrong stays"
        " wrong: a rollback stops new damage, and repairs nothing."
    ),
    input_schema={
        "type": "object",
        "properties": {"service": {"enum": list(ROLLBACK_SERVICES), "description": "The service to roll back."}},
        "required": ["service"],
        "additionalProperties": False,
    },
)

CATALOG: dict[str, ActionSpec] = {spec.name: spec for spec in (ROLLBACK_DEPLOY,)}
