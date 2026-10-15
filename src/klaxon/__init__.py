"""Klaxon: an AI on-call engineer for a payments system (in progress).

So far: a synthetic payments system (the "patient") running in Docker Compose, the money check Klaxon
will use to verify fixes, three faults with known causes, and (week 3) Klaxon's read-only tools, served
over MCP, with the model adapters and the trace recorder its loop will use. The agent loop is not built yet.
"""

__version__ = "0.0.1"
