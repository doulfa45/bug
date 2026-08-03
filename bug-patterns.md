# Bug pattern catalogue — Rust DeFi arbitrage bots

Use this during Phase 5 of the audit methodology (see SKILL.md), and as a lookup any time a Phase 2-4 finding needs a category. Every item here is a *pattern to check for*, not an automatic finding — confirm each one against the actual code before it goes in the report.

## 1. Unit and decimal errors

The single largest source of real financial bugs in DeFi bots, because none of these produce a compiler error or a panic — they just produce a wrong number that looks plausible.

- **Token decimals.** Most ERC-20 tokens use 18 decimals, but not all: USDC and USDT on Polygon use 6, WBTC uses 8. A codebase with a single hardcoded `1e18` or `10u128.pow(18)` used for every token will silently misprice anything that isn't 18-decimal. Check: is decimals read from the token's actual on-chain metadata / a per-token config table, or assumed globally?
- **Rebasing and fee-on-transfer tokens.** If the bot computes trade size from a balance difference (balance after minus balance before), a token that rebases or takes a transfer fee will make that diff wrong even with perfect decimal handling. Check whether the token list includes any of these, and whether the bot accounts for it.
- **Wei / Gwei / Ether.** A factor of 10^9 (Gwei→Wei) or 10^18 (Ether→Wei) apart. Gas prices are conventionally Gwei, balances and amounts are conventionally Wei — a value crossing from a gas-related function into a profit-related function is a place to check the conversion explicitly happened.
- **Percentage vs. basis points vs. raw ratio.** 1%, 100 (bps), and 0.01 are three encodings of the same concept that all fit naturally in a variable called `slippage` or `fee`. Check every arithmetic use of such a variable for which encoding it actually assumes.
- **Price direction.** `price` as token0-per-token1 vs. token1-per-token0 — inverting this doesn't error, it just produces a plausible-looking wrong number. Check against the pool/pair's actual definition (e.g. a Uniswap-v2-style pool's `price0`/`price1` convention) rather than assuming.
- **Cross-pair comparisons.** Comparing raw prices across two DEXs (or two pools) without first normalizing for each pool's own decimals and quote-token convention.

## 2. Numeric and precision errors

- **Overflow / underflow.** Rust panics on overflow in debug builds, wraps silently in release builds, unless the code uses `checked_*`, `saturating_*`, or `wrapping_*` explicitly. Find every raw `+`, `-`, `*` on a balance/price/amount type and check which regime it's actually running in, and whether wrapping (almost never correct for money) is what's actually happening in production.
- **Floating point for money.** `f64` has about 15-17 significant decimal digits — fine for display, risky for exact comparisons near a threshold (`if profit > 0.0` after a floating-point subtraction can go either way right at the boundary). Prefer integer/fixed-point math for anything that gates a decision; flag `f32`/`f64` used in a comparison that decides whether to trade.
- **Division before multiplication.** `a / b * c` loses precision that `a * c / b` wouldn't, when working in integers. Check the order in any formula involving a ratio.
- **Lossy casts.** `as u128`, `as u64`, `as f64` on a `U256` — does the realistic value range actually fit? A cast that's safe for a test fixture can silently truncate for a real large balance.

## 3. Concurrency and timing

- **Stale reads / TOCTOU.** Time between reading a price (or balance, or nonce) and acting on it. Is there an explicit freshness check, or is the read just trusted?
- **Shared mutable state.** `Arc<Mutex<_>>` / `RwLock` — is the lock held across the whole check-then-act sequence, or only across the read, leaving a window for another task to act on the same snapshot?
- **Nonce races.** Concurrent transaction submission fetching "next nonce" independently per task is a classic way for one tx to silently never land, or to get stuck and block everything queued behind it on that account.
- **Unbounded concurrency.** Does the bot cap how many in-flight RPC calls / pending transactions it allows, or can a slow response pile up unbounded work?

## 4. Error handling

- **Swallowed `Result`s.** `.ok()`, `.unwrap_or_default()`, `let _ = result;`, a `match` arm that logs and falls through. Trace what the default value actually is and what happens if downstream code treats it as real data (a defaulted price of `0` being read as "free money," for instance).
- **Panics reachable from external input.** `.unwrap()` / `.expect()` / array indexing on anything derived from an RPC response or on-chain data. A malformed or unexpected response shouldn't be able to crash the whole bot.
- **Retries without limits.** Unbounded retry loops against a flaky RPC can hammer the endpoint or resubmit a transaction that already landed.

## 5. Execution and MEV-adjacent logic

- **Slippage calculated but not enforced.** The bot computes an acceptable slippage tolerance, but does that value actually get passed into the transaction's `amountOutMin` (or equivalent), or does the transaction get built with a default/zero that leaves it unprotected?
- **Gas strategy.** Legacy `gasPrice` vs. EIP-1559 `maxFeePerGas`/`maxPriorityFeePerGas` — mixing the two models, or no cap on either, leaves the bot exposed to a gas spike eating the whole trade's profit (Polygon gas is usually cheap but not immune to spikes).
- **Public mempool exposure.** Does the bot assume private/protected submission it doesn't actually have? Broadcasting an obviously profitable arbitrage transaction to a public mempool invites front-running.
- **Trade sizing vs. price impact.** Does the profit calculation use the quoted price for the bot's full trade size, or does it account for the fact that a large-enough trade moves the price against itself?
- **Profit check ignoring gas, or computing it in the wrong unit** — see section 1.

## 6. Configuration and consistency

- **Network/address mismatches.** Contract and token addresses that don't match the chain ID actually configured (a testnet address surviving into a mainnet config file, or vice versa).
- **Duplicated constants.** The same decimals/threshold/address defined in more than one place in the codebase, with no single source of truth — check whether they can drift out of sync, and whether they already have.
- **Divergent duplicate logic.** Two functions that are each supposed to compute "the same thing" (e.g. a profit calculation used in a simulation path and a separate one used in the live execution path) that have quietly diverged.
- **Secrets and RPC handling.** Are private keys and API keys loaded safely (not logged, not hardcoded)? Is there a fallback RPC endpoint if the primary one is down, or does the whole bot stall?

## 7. Rust-specific footguns

- **`unsafe` blocks.** Is the invariant the block relies on actually upheld at every call site, or only in the cases the author was thinking about at the time?
- **Trait implementations that don't match intended semantics** — a custom `Ord`/`PartialOrd` on a price/amount type that doesn't sort the way callers assume.
- **Cloning on the hot path.** Not a correctness bug on its own, but worth flagging if it affects latency in a strategy where being a block late means losing the opportunity to someone else's bot.

## Polygon-specific notes

- Polygon PoS mainnet is chain ID 137; the Amoy testnet (which replaced the deprecated Mumbai testnet) is chain ID 80002. A hardcoded chain ID anywhere in the codebase is worth checking against which environment the surrounding config actually targets.
- The network's native gas/staking token completed its migration from MATIC to POL; code, comments, or config still referring to "MATIC" as the gas token aren't necessarily wrong (the symbol persisted in a lot of tooling), but it's worth confirming the bot prices gas in whichever token its actual RPC/wallet setup uses.
- Block times are fast (~2 seconds), which shrinks the window for staleness bugs to show up in casual testing but doesn't make them safe — a race that only bites 1 time in 200 will still happen, just less often during a short manual test.
- Common Polygon DEXs a bot in this space typically integrates with include QuickSwap, SushiSwap, and Uniswap v3's Polygon deployment — each with its own fee tiers and pool-price conventions, which is exactly the kind of detail that causes section-1-style bugs when a function written against one DEX's convention gets reused for another without adjustment. Verify current addresses and network parameters against official docs rather than trusting a possibly-stale hardcoded value, since these details do change over time.
