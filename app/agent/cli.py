from __future__ import annotations

import argparse
import os

from app import models  # noqa: F401 - registers SQLAlchemy metadata
from app.agent.providers import OpenAICompatibleProvider
from app.agent.runtime import AgentRuntime
from app.agent.tools import ToolRegistry
from app.database import Database
from app.seed import seed_demo_data


DEFAULT_TASK = (
    "The customer says order ORD-1024 has still not arrived after 10 days. "
    "Check the relevant customer, order, shipment and refund policy. If the order "
    "is eligible, issue the appropriate refund and create a support ticket. "
    "Then report what happened."
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 2 after-sales agent")
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK)
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()

    database = Database(os.getenv("DATABASE_URL", "sqlite:///./after_sales.db"))
    database.create_schema()
    with database.session_factory() as session:
        seed_demo_data(session)
        result = AgentRuntime(
            OpenAICompatibleProvider.from_env(),
            ToolRegistry(session),
            max_steps=args.max_steps,
        ).run(args.task)
        print(result.model_dump_json(indent=2))
    database.dispose()


if __name__ == "__main__":
    main()

