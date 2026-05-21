"""Demo: a planted runaway loop, stopped at $cap.

Run:
    python examples/runaway_loop.py --cap 0.50

Prints one line per call. When the shared pool is exhausted, BudgetExceeded
fires and the worker shuts down clean.

The "agent" here is a stand-in — it has a deliberately malformed tool
response that previously sent the real Hermes loop into unbounded retry.
The skin doesn't care: it just gates every call against the shared pool.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

# Add src/ to path so the example runs without `pip install -e .`.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from hermes_budget_skin import BudgetExceeded, with_budget_skin  # noqa: E402


@dataclass
class FakeUsage:
    usd_cost: float

    def __getitem__(self, k: str) -> float:
        return getattr(self, k)

    def get(self, k: str, default: Any = None) -> Any:
        return getattr(self, k, default)


@dataclass
class FakeResult:
    text: str
    usage: dict[str, float]
    contacted_domains: list[str]


class FakeRunawayAgent:
    """Stand-in for a Hermes Agent that loops on a malformed tool response.

    Each call costs $0.018, contacts api.anthropic.com.
    """

    model = "claude-sonnet-4-5"

    def run(self, prompt: str, **kwargs: Any) -> FakeResult:
        return FakeResult(
            text="(model would have continued)",
            usage={"usd_cost": 0.018},
            contacted_domains=["api.anthropic.com"],
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cap", type=float, default=0.50, help="USD pool cap.")
    args = parser.parse_args()

    agent = with_budget_skin(
        FakeRunawayAgent(),
        usd_cap=args.cap,
        allowed_domains={"api.anthropic.com"},
        domain_fn=lambda r: r.contacted_domains,
    )

    n = 0
    try:
        while True:
            n += 1
            agent.run("loop attempt #%d" % n)
            print(f"[reserve] $0.018 ok, call #{n}")
    except BudgetExceeded as e:
        print(f"[reserve] $0.018 BudgetExceeded — refusing call ({e})")
        print(f"[exit] worker shut down clean after {n} attempts")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
