"""The one place Neo4j drivers are created: the pipeline, the retriever, the MCP server and the evals all
connect through connect().

- Credentials come from .env at the repository root (or from an explicit mapping).
- notifications_min_severity = WARNING: the server only sends WARNING notifications (e.g. an unknown label in a
  query), not INFORMATION ones.
- The driver logs notifications on the "neo4j.notifications" logger. That logger is wired to the process's real
  stderr (sys.__stderr__) and does not propagate, so a notification can never reach stdout -- which the MCP
  server uses for the protocol -- or the rows returned as tool results.
"""

import logging
import os
import sys
from collections.abc import Mapping

from neo4j import Driver, GraphDatabase, NotificationMinimumSeverity

from kg_pipeline import paths

NOTIFICATIONS_LOGGER = "neo4j.notifications"
NOTIFICATIONS_MIN_SEVERITY = NotificationMinimumSeverity.WARNING


def route_notifications_to_stderr() -> None:
    log = logging.getLogger(NOTIFICATIONS_LOGGER)
    if not any(getattr(h, "_kg_stderr", False) for h in log.handlers):
        handler = logging.StreamHandler(sys.__stderr__)  # the real stderr, even if sys.stdout/stderr are redirected
        handler.setFormatter(logging.Formatter("[neo4j notification] %(levelname)s %(message)s"))
        handler._kg_stderr = True
        log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.propagate = False  # never through the root logger, whose handlers might write to stdout


def credentials(env: Mapping[str, str | None] | None = None) -> dict:
    if env is None:
        from dotenv import load_dotenv

        load_dotenv(paths.ENV)
        env = os.environ
    missing = [k for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD") if not env.get(k)]
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} (set them in {paths.ENV.name}; see .env.example)")
    return {"uri": env["NEO4J_URI"], "user": env["NEO4J_USERNAME"], "password": env["NEO4J_PASSWORD"],
            "database": env.get("NEO4J_DATABASE") or None}


def connect(env: Mapping[str, str | None] | None = None) -> Driver:
    """A driver with WARNING-level notifications logged to stderr. Use database() for the session database."""
    route_notifications_to_stderr()
    c = credentials(env)
    return GraphDatabase.driver(c["uri"], auth=(c["user"], c["password"]),
                                notifications_min_severity=NOTIFICATIONS_MIN_SEVERITY)


def database(env: Mapping[str, str | None] | None = None) -> str | None:
    return credentials(env)["database"]
