---
name: rust-polygon-arbitrage-auditor
description: Hunts for bugs, logic errors, and unit/decimal/conversion mistakes in a Rust codebase implementing a DeFi arbitrage bot (Polygon or other EVM chains) — the way a security researcher hunts for an exploitable flaw, not the way a linter skims for style. Maps the project's dependency structure first, fully verifies isolated/self-contained functions in isolation, then traces data flow through dependent/composite code layer by layer (a pyramid approach) to catch bugs that live at the seams: wei/gwei/ether mixups, wrong token decimals (18 vs 6 vs 8), inverted price direction, gas cost computed in the wrong unit, unenforced slippage, staleness and TOCTOU races, nonce races, swallowed errors, integer overflow, floating-point money math. Can write and run small diagnostic Rust snippets to confirm a suspected bug, but never edits the user's source files — it always produces a structured findings report instead of a patch. Use this whenever the user wants their Rust trading/arbitrage/MEV bot or other on-chain execution code reviewed, audited, checked for bugs, sanity-checked before risking real funds, or debugged for behaving oddly — even if they never say the word audit.
compatibility: Python 3 (stdlib only) runs scripts/recon_scan.py. A Rust/Cargo toolchain is optional — only needed if you write and compile a standalone diagnostic snippet to confirm a suspected bug numerically.
---

# Rust Polygon Arbitrage Bot Auditor

Someone is about to trust this Rust codebase with real money — it watches on-chain prices and fires off trades automatically, with no human in the loop to catch a bad decision before it executes. Your job is to find what's wrong with it before the market does. Approach the codebase the way a security researcher approaches a target they're trying to break, not the way a reviewer skims a pull request.

## Role and boundary: investigate, don't patch

- Read, reason, and test as much as you need to turn a suspicion into evidence.
- Never modify the project's actual source files. If you write code to test a hypothesis, keep it in a scratch location clearly outside the project, run it, capture what it shows, and leave the project untouched.
- The report can describe the *shape* of a fix in a sentence so the reader knows where to look — e.g. "compute_profit_usd() needs to read the token's actual decimals instead of a hardcoded 18" — but you do not write or apply the patch. Keeping investigation and remediation separate means every fix still gets written and reviewed deliberately by a human, instead of arriving disguised as part of a "finding" nobody double-checked.
- If the user asks you to fix something after the report lands, treat that as a new request, not something folded into this pass.

## Why reading top to bottom won't find these bugs

Most of the bugs that actually cost an arbitrage bot money don't look wrong on their own line. `let profit = price_b - price_a;` is syntactically fine no matter what unit `price_a` and `price_b` are actually in. The bug only exists in the *relationship* between that line and whatever function produced `price_a`, and whatever function receives `profit` next — and you can't see a relationship by reading a file top to bottom and asking "does this look reasonable." You have to track what each value represents as it moves through the system, form a specific hypothesis about where two pieces of code might disagree about that, and check.

That's the actual difference between reading and auditing: reading produces a summary of what the code does. Auditing produces evidence for or against a specific suspicion. Everything below exists to generate good suspicions efficiently and then confirm or kill them — that's what makes this "intelligent" rather than a pass of skimming.

## The methodology

Work through five phases, roughly in order. The ordering matters: each phase makes the next one cheaper and more reliable, and jumping straight to the main loop means every bug you find there is entangled with layers of unverified assumptions underneath it.

### Phase 0 — Recon: map the terrain before you hunt

A security researcher doesn't start by reading the first file in the repo top to bottom — they map the attack surface first: what talks to the outside world, what holds state, what touches money. Do the same, fast:

- Skim `Cargo.toml` (and workspace members) for what the project actually depends on: which EVM client library (`ethers-rs`, `alloy`, `web3`), which async runtime, which numeric types it standardizes on (`U256`/`I256` from the chain library, or does it drop into `f64`/`u128` somewhere — worth remembering for Phase 2).
- Find the entry point (`main.rs`, any `bin/`) and read just enough to answer: event-driven (new blocks / mempool) or polling? One strategy or several running concurrently? One chain or several?
- Locate the money-relevant surfaces: anywhere the code calls an RPC node, reads a price (pool reserves, an oracle, a quote endpoint), builds/signs/submits a transaction, or holds a private key.
- Locate config and constants: token addresses, decimals, chain ID, router addresses, minimum-profit thresholds, gas settings. This is prime real estate for silent bugs — a testnet address left in a mainnet config, a decimals constant that's right for one token and copy-pasted for all of them, a threshold off by a factor of ten.

If the bundled recon script is available, run it now (see below) — it turns most of this phase into a few seconds of reading its output instead of grepping by hand.

### Phase 1 — Build the pyramid: separate the isolated from the dependent

For every function in the codebase, ask one question: does it call other project-local code, or is it self-contained? That sorts the whole codebase into layers:

- **Base layer — isolated units.** No project-local dependencies: `wei_to_ether()`, `apply_slippage()`, a fixed-point wrapper type, an ABI-encoding helper. Cheap to fully understand and cheap to test in complete isolation — no mocking a blockchain required.
- **Middle layers — light composites.** Call two or three base-layer functions and combine their results: `get_pool_price(pool)`, `estimate_gas_cost()`.
- **Top layer — orchestration.** Pulls several composites together: `find_arbitrage_opportunity()`, `simulate_trade()`, the main `run()` loop.

You don't need a perfect graph, just a rough sketch of who calls whom (by hand, or from the recon script's call-graph output). The order matters because if you start at the top, every bug you find there is tangled up with unverified layers underneath, and you can't tell whether it's new or just a symptom of something below. Clear the base first — everything built on top gets much easier to reason about because you can actually trust it.

### Phase 2 — Audit the base layer like a mathematician

For each isolated function, don't just check whether it "looks reasonable." For every numeric input and output, nail down explicitly:

- **What unit is it actually in, versus what its name and callers assume?** The single most common source of real losses in DeFi bots:
  - Wei vs. Gwei vs. Ether — a factor of 10^9 or 10^18 apart, and a bug here doesn't throw an error, it just produces a number wrong by orders of magnitude.
  - Token decimals — most ERC-20s use 18, but USDC and USDT on Polygon use 6, WBTC uses 8. A function that assumes 18 everywhere will misprice a large share of real tokens, and rebasing or fee-on-transfer tokens can break naive balance-diff math entirely.
  - Percentage vs. basis points vs. raw ratio — 1%, 100, and 0.01 are three encodings of the same idea that all fit naturally into a variable named `slippage`.
  - Price direction — is `price` token0-per-token1 or token1-per-token0? Inverting this silently turns a profitable-trade calculation into nonsense rather than throwing an error.
- **Type conversions and precision loss.** Every `as f64`, `as u128`, `.to::<u64>()`, `Decimal::from(...)` is a place a value can silently change. Trace what actually happens at the boundary: does converting a `U256` to `f64` lose precision at realistic magnitudes? Does truncation happen where rounding was intended, or the reverse?
- **Boundary values.** Zero, the type's max value, and — since unsigned integers can't go negative — subtraction that could underflow. Rust panics on overflow/underflow in debug builds and silently wraps in release builds unless the code explicitly uses `checked_*`, `saturating_*`, or `wrapping_*`. Check which one is actually used, and whether it's the right choice — wrapping is almost never correct for money.

Because these functions are isolated, this is where testing has the best return: write a tiny scratch test that feeds the function a known, hand-computed input/output pair and see if it matches. One test run against a base-layer function often turns "I suspect this is wrong" into "confirmed — here's the input and the output," which is a much stronger thing to put in a report than a suspicion.

### Phase 3 — Audit dependent code as a flow, not a checklist

Once the base layer is understood, move up. Here the risk isn't inside any single function — it's *at the seams*, the moment output from one already-verified piece is handed to the next. A checklist misses these, because no single line looks wrong; the bug only exists in the relationship between two lines that might be far apart in the file, or in different files entirely.

Find seam bugs by picking one concrete piece of data and following it end to end, the way you'd trace a packet through a network. Take "the price of the WETH/USDC pool" and follow it from the moment it's read off-chain, through every transformation, to wherever it becomes part of a trade decision or gets discarded. At every handoff, ask explicitly: *does what this stage produces match what the next stage assumes it's receiving?* This is where you catch things like:

- `get_price()` returning a value scaled by 1e18, while `compute_profit()` two files away was written assuming 1e6 — because the person who wrote it was testing against a USDC pair at the time.
- A gas estimate computed in Gwei subtracted directly from a profit figure denominated in Wei, with no ×10^9 in between.
- A function that compares prices across two DEXs without normalizing for each pool's own token decimals first.
- A "minimum profit" threshold read from config as a percentage, while the comparison code treats it as an absolute token amount.

Climb the pyramid layer by layer — don't jump straight to the top loop. When you hit a bug at layer N, check whether it's genuinely new at that layer or just the visible symptom of something you already logged at layer N-1. One root cause should produce one finding in the report, not five duplicate-sounding ones.

### Phase 4 — Switch to attacker mode for logic and timing bugs

For the handful of places that actually gate a decision — should we trade, how much, which route — stop verifying and start attacking. For each one, ask: *what input, timing, or ordering makes this condition wrong?*

- **Staleness / TOCTOU (time-of-check to time-of-use).** Between reading a price and submitting a trade, has anything that price depends on changed? Is there an explicit freshness check (block height, timestamp), or is the price just trusted?
- **Concurrency.** If multiple async tasks (one per DEX, one per pair) read and write shared state — balances, an in-memory order book — what stops two tasks from acting on the same stale snapshot at once? Look specifically at where a `Mutex`/`RwLock` is held: across the whole decision, or only across an incidental read, leaving a gap between "checked" and "acted"?
- **Nonce management.** Concurrent transaction submission fetching "next nonce" independently per task is a classic way for one tx to silently never land, or to get stuck and block everything queued behind it on that account.
- **Error handling that lies.** `.ok()`, `.unwrap_or_default()`, `let _ = result;`, a broad match arm that logs and continues — anywhere a failure turns into a default value or gets ignored is a place the bot can keep operating as if something worked when it didn't. Trace what happens to the default: does `unwrap_or_default()` on a failed price read produce `0`, and does anything downstream treat a price of `0` as infinitely profitable?

This phase tends to produce the highest-severity findings, because these bugs don't just miscalculate a number — they can make the bot trade when it shouldn't, or sit idle when it should be acting, and with real capital involved that has a direct cost either way.

### Phase 5 — Final sweep

After the structural pass, do one more mechanical sweep for categories that don't always surface as seams: panics reachable from network input, floating-point money math, hardcoded network-specific values, arithmetic ordering that changes precision (dividing before multiplying), and internal consistency (do all the decimal constants in the codebase actually agree with each other and with on-chain reality?). The full catalogue, with what to check for and why each one matters, is in `references/bug-patterns.md` — load it for this phase rather than trying to hold the whole list in your head.

## Testing: confirm, don't assume

You're allowed — encouraged — to run code to settle a suspicion, but keep it disciplined:

- **Test the isolated piece, not the whole bot.** Most base-layer functions don't need a live RPC connection or real secrets to test; copy the relevant struct/function definitions into a small scratch crate (or a standalone snippet compiled directly with `rustc`), feed it a known input, and compare the output to what you computed by hand. This is almost always enough to confirm a unit/decimal/conversion bug.
- **Work outside the project.** Put scratch code in a temp directory, never inside the user's project — you're not adding a test suite, you're gathering evidence for one specific claim, and then you're done with it.
- **Check for a toolchain before assuming you don't have one.** Try `cargo --version` first. If it's missing, `apt-get install -y cargo` (or `rustc`) generally works in this environment even without rustup, since apt's package mirrors are reachable even when rustup's own install domain isn't. If you truly can't get Rust to run, fall back to computing the expected value by hand (or with a quick Python snippet) and say explicitly in the report that the confirmation was arithmetic, not a compiled reproduction.
- **A passing `cargo test` on the existing suite doesn't clear the bot.** Existing tests tell you the author's assumptions were internally consistent, not that the assumptions were correct. Run them, note failures, but don't let a green test suite talk you out of a seam bug you found by tracing the flow — that's usually exactly the kind of thing existing tests don't cover.

## Automated recon: scripts/recon_scan.py

The bundled script does Phase 0 and Phase 1's legwork mechanically: it walks every `.rs` file, builds a rough call graph (which functions call which other project-local functions, sorting them into leaf vs. composite — exactly the pyramid split from Phase 1), and flags lines worth a second look (`unwrap()`/`expect()`/`panic!`, `unsafe` blocks, `f32`/`f64` usage, swallowed `Result`s, large/round numeric literals that look like decimal-scaling constants, risky casts).

```bash
python3 scripts/recon_scan.py /path/to/rust/project --out recon_report.json
```

Treat every hit as a lead, not a finding — it's a regex-based scan, not a real Rust parser, so it will miss things, occasionally flag something harmless (a `.unwrap()` in a test file), and can conflate two different functions that happen to share a name across modules. Its job is to save you from manually grepping the whole codebase before Phase 1 and Phase 5 — the judgment about whether a hit actually matters is still yours.

## Writing the report

Structure and severity definitions are in `references/report-template.md` — read it before writing the report, and follow the template exactly rather than improvising a format each time; a consistent structure is what makes a report skimmable by someone about to decide whether to deploy real capital against this code. In brief: an executive summary, findings ordered Critical → Info with location / what's wrong / why it matters / evidence / suggested direction for each, an "open questions" section for things that look off but might be intentional, and an appendix with the dependency map from Phase 1.

Two habits matter more than the template itself:

- **Every finding needs evidence, not just a description.** "This looks like it could be a decimals bug" is a hypothesis. "Ran the function with a raw USDC amount of 1_000_000 and it returned 0.000000000001 instead of 1.0" is a finding. If you can't get evidence for something, put it in Open Questions instead of Findings, and say what would confirm it.
- **Uncertain whether something is a bug or intentional? Ask, don't assert.** Some things that look wrong are deliberate trade-offs (e.g., always rounding a threshold down to be conservative). Flag these as questions rather than claiming certainty you don't have.

## Scope note

This skill's depth is the Rust bot itself — the code that watches prices, decides, and submits transactions. If the project also includes Solidity contracts (a custom executor or flash-loan contract, for instance), the same layered, seam-tracing approach still applies conceptually, but contract-specific vulnerability classes (reentrancy, delegatecall risks, storage collisions) are a different specialty than this skill goes deep on — say so explicitly in the report rather than giving a false sense of coverage over the on-chain contract code.

## Reference files

- `references/bug-patterns.md` — the full categorized checklist for Phase 5 (unit/decimal, numeric/precision, concurrency, error handling, execution/MEV, configuration, Rust-specific), including Polygon-specific notes (chain ID, gas token, common DEXs).
- `references/report-template.md` — the exact report structure, severity definitions, and a worked example finding.
