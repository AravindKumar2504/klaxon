"""Klaxon: an AI on-call engineer for a payments system (in progress).

So far: a synthetic payments system (the "patient") running in Docker Compose, the money check Klaxon
will use to verify fixes, three faults with known causes, and (week 3) Klaxon's read-only tools, served
over MCP, the model adapters, the trace recorder, and a first agent loop that investigates an alert and
ends in a diagnosis. It acts on nothing yet: a fix is only proposed.
"""

__version__ = "0.0.1"
