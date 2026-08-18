"""Hivemind client: talk to a hivemind server (graph, artifacts, tools, guide)."""
from .client import Client, HivemindError

__all__ = ["Client", "HivemindError"]
__version__ = "0.1.0"
