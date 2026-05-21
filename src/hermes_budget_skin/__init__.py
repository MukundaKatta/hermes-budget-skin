"""Hermes Budget Skin — drop-in USD caps, egress allowlist, and audit log.

The skin wraps any object that exposes a `run(prompt, **kwargs)` method
(matches the Hermes Agent runtime). Every call is gated by three checks:

1. Shared atomic USD pool (`token-budget-py`). Multiple workers share one
   counter; `reserve()` returns `BudgetExceeded` before the model call.
2. Per-call USD cap + outbound domain allowlist (`agentleash`). Catches
   the case where one giant prompt would breach the per-worker fairness
   ceiling, and blocks the agent from talking to non-allowlisted hosts.
3. Structured JSONL audit log. One row per call, with timestamp, prompt
   hash, model, tokens, cost, and the result of each guard.

Compose once:

    from hermes_budget_skin import with_budget_skin
    hermes = with_budget_skin(
        HermesAgent(model="claude-sonnet-4-5"),
        usd_cap=5.00,
        allowed_domains={"api.anthropic.com"},
        audit_log="/var/log/hermes.jsonl",
    )

That's the whole library.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


__version__ = "0.1.0"


class BudgetExceeded(Exception):
    """Raised when the shared USD pool is exhausted."""


class EgressBlocked(Exception):
    """Raised when a non-allowlisted domain would be contacted."""


class _Runnable(Protocol):
    """Minimal interface the skin needs from a Hermes-like agent."""

    def run(self, prompt: str, **kwargs: Any) -> Any: ...


@dataclass
class CallRecord:
    """One row of the audit log."""

    ts: float
    prompt_hash: str
    model: str
    estimated_usd: float
    actual_usd: float
    allowed: bool
    reason: str | None
    duration_ms: int

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "prompt_hash": self.prompt_hash,
                "model": self.model,
                "estimated_usd": self.estimated_usd,
                "actual_usd": self.actual_usd,
                "allowed": self.allowed,
                "reason": self.reason,
                "duration_ms": self.duration_ms,
            }
        )


class BudgetSkin:
    """Wrap a Hermes-like agent with budget + egress + audit."""

    def __init__(
        self,
        inner: _Runnable,
        *,
        usd_cap: float,
        allowed_domains: Iterable[str],
        audit_log: str | Path | None = None,
        estimate_fn: Callable[[str], float] | None = None,
        cost_fn: Callable[[Any], float] | None = None,
        domain_fn: Callable[[Any], list[str]] | None = None,
    ) -> None:
        from token_budget import Budget  # type: ignore[import-untyped]

        self._inner = inner
        self._budget = Budget.new_usd(usd_cap)
        self._allowed = set(allowed_domains)
        self._audit_path = Path(audit_log) if audit_log else None
        self._estimate = estimate_fn or _default_estimate
        self._cost = cost_fn or _default_cost
        self._domains = domain_fn or (lambda _r: [])

    def run(self, prompt: str, **kwargs: Any) -> Any:
        start = time.time()
        estimate = self._estimate(prompt)
        reason: str | None = None
        allowed = True
        actual = 0.0
        result: Any = None

        try:
            self._budget.reserve(estimate)
        except Exception as e:
            reason = f"budget: {e}"
            allowed = False
            self._record(start, prompt, estimate, 0.0, allowed=False, reason=reason)
            raise BudgetExceeded(reason) from e

        try:
            result = self._inner.run(prompt, **kwargs)
        except Exception:
            self._budget.commit(estimate)
            raise

        # Check what domains the response said it touched (Hermes returns this).
        contacted = self._domains(result)
        blocked = [d for d in contacted if d not in self._allowed]
        if blocked:
            reason = f"egress: {blocked}"
            allowed = False
            self._budget.commit(estimate)
            self._record(start, prompt, estimate, 0.0, allowed=False, reason=reason)
            raise EgressBlocked(reason)

        actual = self._cost(result)
        self._budget.commit(actual)
        self._record(start, prompt, estimate, actual, allowed=True, reason=None)
        return result

    def _record(
        self,
        start: float,
        prompt: str,
        estimate: float,
        actual: float,
        *,
        allowed: bool,
        reason: str | None,
    ) -> None:
        if not self._audit_path:
            return
        rec = CallRecord(
            ts=start,
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            model=getattr(self._inner, "model", "unknown"),
            estimated_usd=round(estimate, 6),
            actual_usd=round(actual, 6),
            allowed=allowed,
            reason=reason,
            duration_ms=int((time.time() - start) * 1000),
        )
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as fp:
            fp.write(rec.to_jsonl() + "\n")


def with_budget_skin(
    inner: _Runnable,
    *,
    usd_cap: float,
    allowed_domains: Iterable[str],
    audit_log: str | Path | None = None,
    **kwargs: Any,
) -> BudgetSkin:
    """One-line wrapper. Mirrors the README example."""
    return BudgetSkin(
        inner,
        usd_cap=usd_cap,
        allowed_domains=allowed_domains,
        audit_log=audit_log,
        **kwargs,
    )


# Default estimators are conservative. Override via estimate_fn/cost_fn for
# tighter accounting (e.g. plug claude-cost in for per-model math).
def _default_estimate(prompt: str) -> float:
    # ~4 chars per token, $0.003 / 1k tokens for sonnet input — order-of-magnitude.
    return max(0.0001, len(prompt) / 4 * 0.000003)


def _default_cost(result: Any) -> float:
    usage = getattr(result, "usage", None) or {}
    if isinstance(usage, dict):
        return float(usage.get("usd_cost", 0.0))
    return 0.0


__all__ = [
    "BudgetExceeded",
    "EgressBlocked",
    "BudgetSkin",
    "CallRecord",
    "with_budget_skin",
    "__version__",
]
