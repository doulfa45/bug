# Report template

Use this structure for every audit report produced by this skill. A consistent structure is what makes a report skimmable by someone about to decide whether to deploy real capital against this code — don't improvise a different format per run.

## Severity definitions

- **Critical** — directly and predictably causes loss of funds, or the bot trading when it shouldn't (or not trading when it should) under normal operating conditions — not a contrived edge case.
- **High** — wrong result under realistic but not universal conditions (a specific token, a specific market state), or a Critical-class mechanism that needs a somewhat unusual but plausible precondition to trigger.
- **Medium** — incorrect behavior confined to an edge case, or a correctness issue with no direct money impact (a metric logged wrong, a display value off).
- **Low** — a robustness issue that isn't causing wrong behavior today but could become one later (a panic on malformed input that hasn't been observed in practice but isn't ruled out either).
- **Info** — something that looks odd and deserves a human's attention, but you couldn't confirm it's actually wrong. Goes in Open Questions, not Findings.

## Structure

```markdown
# [Project name] — Audit Report

Date: [date]. Scope: [commit / version / directory audited, if known].

## Executive summary

One paragraph: what was audited, how, and the headline counts (N Critical, N High,
N Medium, N Low, N Info). If something was out of scope or you ran out of time
before finishing a section, say so here rather than letting the reader assume
full coverage.

## Methodology

2-4 sentences: roughly how many modules/functions, how the leaf/composite split
broke down, which phases were completed.

## Findings

One entry per finding, ordered Critical -> Info:

### [SEVERITY] Short descriptive title

**Location:** `path/to/file.rs:123`, function `fn_name()`
**Category:** Unit/Conversion | Numeric/Precision | Concurrency | Error handling |
              Execution/MEV | Configuration | Rust footgun
**What's wrong:** 2-4 sentences, concrete. State it plainly if you're sure — don't
hedge a confirmed finding.
**Why it matters:** The actual mechanism by which this causes a problem — a wrong
trade, a crash, a silent no-op, a misleading log line. Be specific, not "this could
cause issues."
**Evidence:** How you know. A test run with concrete input/output, or a concrete
flow trace naming the exact two places that disagree (file:line on both sides).
**Suggested direction:** One or two sentences on what a fix would need to address.
Not a diff, not applied code — enough for the reader to know where to look next.

## Open questions

Things that look wrong but might be intentional, or where you lacked the business
context to be sure. Phrase these as questions.

## Appendix: dependency map

The leaf/composite layering used to structure the audit, so the reader can see
what was covered and in what order, and a future audit doesn't have to rebuild it
from scratch.
```

## Worked example

To calibrate tone and level of detail:

### [CRITICAL] Gas cost computed in Gwei, subtracted from a profit denominated in Wei

**Location:** `src/strategy.rs:18`, function `evaluate_opportunity()`
**Category:** Unit/Conversion
**What's wrong:** `estimate_gas_price_gwei()` returns a value in Gwei. `evaluate_opportunity()` multiplies it directly by a gas-unit count and subtracts the result from `gross_profit_wei`, which is genuinely denominated in Wei. The multiplication never applies the ×10^9 Gwei-to-Wei conversion.
**Why it matters:** The subtracted "gas cost" ends up roughly a billion times smaller than the real gas cost in Wei. `should_execute_trade()` calls this function and fires whenever the result is positive — so any opportunity whose real (correctly converted) gas cost would have exceeded the gross profit still reads as profitable, and the bot executes a trade that loses money net of gas.
**Evidence:** With `gross_profit_wei = 2e16`, gas price `100` Gwei, and `500_000` gas units: the real gas cost is `100 * 1e9 * 500_000 = 5e16` Wei, which exceeds the gross profit (should not trade). The current code computes `100 * 500_000 = 5e7` and subtracts that instead, leaving a "profit" of `~2e16` (reads as clearly profitable). Reproduced with a standalone scratch snippet using these exact numbers.
**Suggested direction:** `evaluate_opportunity()` needs to convert the Gwei gas price to Wei (×10^9) before multiplying by gas units, to match the unit `gross_profit_wei` is already in.
