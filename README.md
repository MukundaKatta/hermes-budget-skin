# hermes-budget-skin

[![PyPI](https://img.shields.io/pypi/v/hermes-budget-skin.svg)](https://pypi.org/project/hermes-budget-skin/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![DEV Challenge](https://img.shields.io/badge/DEV-%23hermesagentchallenge-D4A853.svg)](https://dev.to/challenges/hermes-agent-2026-05-15)

**Drop-in USD caps + egress allowlist + structured audit log for the Hermes Agent runtime.**

The week before this submission landed, three workers running in parallel
burned through $40 of Claude budget in 18 minutes because each one had
its own $5 cap and there was no shared counter. Three caps × no shared
counter = a $15+/minute ceiling. I wrote up the story
[here](https://dev.to/mukundakatta/how-one-bad-prompt-burned-40-of-my-claude-budget-in-18-minutes-lha).

This library is the fix, composed of three pieces:

| Piece              | What it does                                   |
|--------------------|------------------------------------------------|
| `token-budget-py`  | Shared atomic USD pool. `reserve()`→`commit()` |
| egress allowlist   | Outbound domain allowlist (built in, no deps)  |
| structured logging | One JSONL row per call, with cost + outcome    |

Wrap Hermes once:

```python
from hermes_budget_skin import with_budget_skin
from hermes_agent import HermesAgent  # whichever package name Hermes ships under

hermes = with_budget_skin(
    HermesAgent(model="claude-sonnet-4-5"),
    usd_cap=5.00,
    allowed_domains={"api.anthropic.com", "google.com"},
    audit_log="/var/log/hermes-audit.jsonl",
)

result = hermes.run("Summarize this PDF and email the gist to ops.")
```

That's it. Every call now:

1. Reserves estimated cost against a shared pool. `BudgetExceeded` fires
   *before* the model call when the pool is exhausted.
2. Checks contacted domains against the allowlist after the call.
   `EgressBlocked` fires if the agent tried to talk to anything else.
3. Writes a JSONL audit row with timestamp, prompt hash, model, estimated
   USD, actual USD, allow/deny outcome, and reason.

## Demo: a planted runaway loop

```bash
git clone https://github.com/MukundaKatta/hermes-budget-skin
cd hermes-budget-skin
pip install -e .[dev]
python examples/runaway_loop.py --cap 0.50
```

Output:

```
[reserve] $0.018 ok, call #1
[reserve] $0.018 ok, call #2
...
[reserve] $0.018 ok, call #27
[reserve] $0.018 BudgetExceeded — refusing call
[exit] worker shut down clean after 28 attempts
```

No bill alert on Monday morning.

## Install

```bash
pip install hermes-budget-skin
```

Pulls in `token-budget-py` as its only runtime dep (MIT, on PyPI). The
egress allowlist is a plain set check built into this package.

## How it composes

```
+------------------+    +-----------------+    +------------------+
| HermesAgent.run  |--->| BudgetSkin.run  |--->| (3 sequential    |
|  (user-facing)   |    | (this library)  |    |  guard checks)   |
+------------------+    +-----------------+    +------------------+
                                                       |
                                +----------------------+----------------------+
                                v                      v                      v
                       +---------------+       +---------------+       +---------------+
                       | reserve()     |       | egress check  |       | audit write   |
                       | token-budget  |       | allowlist set |       | JSONL append  |
                       +---------------+       +---------------+       +---------------+
```

Each guard is failure-mode-isolated:

- **Reserve fails** → `BudgetExceeded`, no model call, pool unchanged.
- **Egress fails** → `EgressBlocked`, model call already happened, refunded.
- **Both pass** → commit actual cost, append audit row, return result.

## What it doesn't do

- **Doesn't bill you.** If Anthropic charged for a call before the cap
  fired, you paid for that one. The point is to stop the *next* call.
- **Doesn't replace prompt-level termination conditions.** The best fix
  is "agent stops because the task is done." This is the fence around
  what happens when that fails.
- **Doesn't replace per-tenant caps.** A worker-level cap is necessary
  but not sufficient. If you're multi-tenant, you also want a route-
  level cap. This skin is the per-worker layer.
- **Doesn't reach the network itself.** All network IO happens inside
  the wrapped agent. The skin just gates and observes.

## Tests

```bash
pip install -e .[dev]
pytest
```

8 tests cover the happy path, budget denial, egress denial, egress refund,
audit log contents, budget deny-row audit, egress deny-row audit, and the
default estimator. All pass clean.

## Related

- `token-budget-py` — https://pypi.org/project/token-budget-py/
- `claude-cost` (Rust, per-model spend math) — https://crates.io/crates/claude-cost
- The dev.to article that started this — [How one bad prompt burned $40 of my Claude budget in 18 minutes](https://dev.to/mukundakatta/how-one-bad-prompt-burned-40-of-my-claude-budget-in-18-minutes-lha)

## License

MIT.
