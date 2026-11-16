from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any

from pydantic import ValidationError

from .config import load_config, load_dotenv
from .pipeline import make_plan, run
from .state import StateStore

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor flight and travel deals")
    parser.add_argument("--config", default="config.yaml", help="YAML configuration file")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="Validate YAML and environment overrides")
    validate.set_defaults(action="validate-config")

    plan = subparsers.add_parser("plan", help="Print planned queries without using network or quota")
    plan.set_defaults(action="plan")

    execute = subparsers.add_parser("run", help="Run the due checks or a selected mode")
    execute.add_argument(
        "--mode",
        choices=["due", "all", "promotions", "discovery", "fares", "digest", "source-check"],
        default="due",
    )
    execute.set_defaults(action="run")
    return parser


def _plan_json(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "travelpayouts": [asdict(query) for query in plan["travelpayouts"]],
        "serpapi": [asdict(query) for query in plan["serpapi"]],
        "serpapi_used_this_month": plan["serpapi_used_this_month"],
        "serpapi_monthly_cap": plan["serpapi_monthly_cap"],
    }


async def _async_main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.action == "validate-config":
        print("Configuração válida.")
        return 0

    store = StateStore(config.storage.state_path)
    if args.action == "plan":
        print(json.dumps(_plan_json(await make_plan(config, store)), ensure_ascii=False, indent=2))
        return 0

    result = await run(config, store, args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    try:
        status = asyncio.run(_async_main(args))
    except (OSError, ValueError, ValidationError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        status = 2
    raise SystemExit(status)
