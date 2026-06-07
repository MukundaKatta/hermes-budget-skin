"""Tests for the Hermes Budget Skin.

Uses a stand-in agent — no real Hermes needed for the unit tests.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hermes_budget_skin import BudgetExceeded, EgressBlocked, with_budget_skin


@dataclass
class FakeResult:
    text: str
    usage: dict[str, float]
    contacted_domains: list[str]


class FakeAgent:
    model = "claude-sonnet-4-5"

    def __init__(self, cost_per_call: float = 0.018, domains: list[str] | None = None) -> None:
        self._cost = cost_per_call
        self._domains = domains or ["api.anthropic.com"]
        self.calls = 0

    def run(self, prompt: str, **kwargs: Any) -> FakeResult:
        self.calls += 1
        return FakeResult(
            text=f"reply to: {prompt}",
            usage={"usd_cost": self._cost},
            contacted_domains=self._domains,
        )


def _agent(**kw: Any) -> FakeAgent:
    return FakeAgent(**kw)


def _wrap(agent: FakeAgent, **kw: Any) -> Any:
    return with_budget_skin(
        agent,
        usd_cap=kw.pop("usd_cap", 1.0),
        allowed_domains=kw.pop("allowed_domains", {"api.anthropic.com"}),
        domain_fn=lambda r: r.contacted_domains,
        **kw,
    )


def test_calls_pass_under_cap() -> None:
    agent = _agent()
    skin = _wrap(agent, usd_cap=1.0)
    for _ in range(5):
        skin.run("hello")
    assert agent.calls == 5


def test_cap_blocks_further_calls() -> None:
    agent = _agent(cost_per_call=0.30)
    skin = _wrap(agent, usd_cap=0.50)
    skin.run("first")  # 0.30 reserved + committed
    # Second is fine: 0.30 + 0.30 = 0.60 — but the reserve is checked.
    # token-budget-py treats the reserve as exact, so 0.30 reserve succeeds
    # only if pool has 0.30 left. Pool started at 0.50, first commit refunds
    # to 0.50-0.30=0.20 left. Second reserve of 0.30 must fail.
    with pytest.raises(BudgetExceeded):
        skin.run("second")
    assert agent.calls == 1


def test_egress_blocks_disallowed_domain() -> None:
    agent = _agent(domains=["sketchy.example.com"])
    skin = _wrap(agent, allowed_domains={"api.anthropic.com"})
    with pytest.raises(EgressBlocked):
        skin.run("hello")


def test_audit_log_writes_jsonl_rows(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    agent = _agent()
    skin = _wrap(agent, audit_log=log)
    skin.run("hello")
    skin.run("world")
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["model"] == "claude-sonnet-4-5"
    assert row["allowed"] is True
    assert row["actual_usd"] > 0


def test_audit_log_records_denials(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    agent = _agent(cost_per_call=2.0)
    skin = _wrap(agent, usd_cap=0.10, audit_log=log)
    with pytest.raises(BudgetExceeded):
        skin.run("too big")
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["allowed"] is False
    assert row["reason"].startswith("budget:")


def test_default_estimator_is_conservative() -> None:
    # Very long prompt should still estimate something nonzero.
    from hermes_budget_skin import _default_estimate

    assert _default_estimate("x" * 100_000) > 0
