# Multichain token manager — persistent project reference

Last updated: 2026-11-06. **Part 1 only: shared decision logic.** Maintain this
document in every later part, including actual commands, results and blockers.

## Product scope

The eventual product is a WhatsApp agent that manages one lending position per
user across Ethereum Sepolia and Base Sepolia. The user approves a versioned
policy with a fixed deadline, permitted pools/token, protected idle reserve,
maximum amount/fees, minimum net benefit, freshness bounds and cooldown. The
objective is better **expected earnings after incremental costs**, within those
rules. Earnings are estimates, never promises. Beginners and experienced users
have the same capabilities; explanations and exposed settings may differ.

This first version has one test token (logical asset `CCIP-BnM`) and one mock
lending pool slot on each chain. Slots are identifiers, not deployed contracts.
No leverage, borrowing, swaps, automated market maker liquidity, yield forecasts
or unrestricted protocol discovery is included.

The four parts remain:

1. **Implemented:** deterministic shared financial rules, typed application/CRE
   boundary, labelled arithmetic fixtures and local tests.
2. Not started: testnet pool contracts, observations and CRE dry runs.
3. Not started: CCIP execution, tracking and recovery.
4. Not started: WhatsApp lending integration and scheduling.

Part 1 did not broadcast transactions, create wallets, change existing wallet
approval behavior, connect messaging, build a dashboard or deploy a workflow to
a decentralized oracle network (DON).

## Existing architecture and preservation decisions

Inspection covered the root README, `docs/ARCHITECTURE.md`, package manifests,
`agent/engine.py`, `agent/wallet.py`, `agent/mobile_wallet.py`, task state/storage,
providers, WhatsApp adapter/docs, CCIP code/tests and contract dependency lock.
No repository `AGENTS.md` file was found; the user's supplied instructions apply.
The initial Git worktree was clean. Existing application and contract files are
unchanged; npm scripts/dependencies and a README pointer are the integration changes.

- **Python agent:** chat and background tasks share the inference/tool loop.
  Exact incoming user approvals are handled before inference. Keep that approval
  boundary; a model may explain or propose policy, but may not supply market
  observations, assert approval or override financial rules.
- **Wallets:** existing encrypted agent wallets and MetaMask signature approval
  are preserved. No wallet initialization or signing was needed here.
- **Storage:** Arkiv SDK 1.0.1 and the existing encrypted checkpoint bridge are
  already integrated. Arkiv is task authority; SQLite is a local execution cache.
  Reuse that boundary for later approved policies, immutable run records and
  operation recovery. Part 1 adds no persistence writes or migrations.
- **Inference:** configured roles use OpenAI through the existing provider
  abstraction. No 0G integration was found in inspected agent/integration code
  or manifests. No inference provider was replaced or newly added.
- **WhatsApp:** the Redis/sidecar adapter and delivery recovery already exist.
  Part 4 can call the shared module through a JSON boundary. Part 1 does not add
  a lending chat tool, start a sidecar or send messages. Documentation and source
  inspection do not establish the current phone/session connection state.
- **CCIP:** existing contracts, simulation and payment recovery are preserved.
  Their presence does not establish lending execution or current lane support.

## New components and data flow

`lending/schema.ts` defines runtime validation and TypeScript types.
`lending/decision.ts` exports `decide(policy, snapshot, clock)`, the **single
implementation of financial rules**. `lending/run.ts` exports
`evaluateRun(input, clock)`, which validates and binds application/CRE requests.
`lending/networks.ts` keeps static deployment slots separate.
`lending/fixtures.ts` supplies frozen arithmetic fixtures. The command-line
example and tests live outside the reusable module.

Intended later flow:

1. The application authenticates the user, verifies wallet binding, loads the
   exact stored approval and selects that user's one managed position.
2. Approved adapters collect rates, balances, source withdrawal availability,
   destination deposit capacity, valuations and disjoint fee quotes. They record
   provenance and observation times. Onchain adapters must additionally retain
   chain/block/finality evidence in the referenced observation record.
3. The application creates one immutable input with a run ID and candidate
   operation ID. It sends that input as trigger data or fetches its immutable
   record by authenticated reference. **Never rewrite a common workflow config
   file with the current user's data.**
4. The application or CRE calls this same evaluator. A later CRE adapter should
   inject its consensus clock (`runtime.now()`), converted to integer Unix
   seconds. There are no wallet, RPC, inference, Node built-ins or ambient clock
   calls in the shared financial module. Financial operations use `bigint`.
5. Persist the bound decision and its input. A later execution layer must
   atomically claim the operation, recheck approval/version and current balances,
   and journal effects before execution. It must record real time in transit.

`move` is a recommendation, **not** a signature, permission or transaction claim.
The pure function cannot lock concurrent calls. Pending-operation checks stop a
new recommendation when the input contains an unresolved operation; atomic
per-user/position admission belongs to the later execution/storage layer.

## Typed per-run interface, version 1

All monetary quantities, token amounts, APR basis points and timestamps cross
JSON as canonical unsigned decimal strings (bounded to uint256). Calculations
can return signed strings. Token decimal counts are bounded integers; monetary
valuation precision is six decimals. There is no floating-point financial math.

`RunInput` includes:

- `interfaceVersion`, `runId` and `networkConfigVersion`;
- `context`: user, wallet, verified wallet-binding reference and policy version;
- `observation`: snapshot ID, assembly timestamp and immutable evidence reference;
- `operationId`: explicit null for evaluation without a candidate, otherwise an
  application-assigned identifier. A move result requires this identifier;
- the approved policy and the complete market/position snapshot.

`createRunInput` validates, copies and recursively freezes the request. TypeScript
also exposes recursive readonly types. The result repeats the run/configuration,
identity, policy version, observation and operation context, alongside the
decision. It is recursively frozen as well. Schema-invalid requests return
`blocked/INVALID_RUN_INPUT` with null binding fields: an invalid ID is not echoed
as an admitted run. Valid envelopes with inconsistent context return a bound
blocked result. Both boundary and direct `decide` validation include field-level
issue paths to identify missing or invalid data.

**Trust boundary:** validation checks structure, ownership/context consistency,
units and policy limits; it does not authenticate arbitrary supplied JSON. The
caller must resolve `walletBindingId`, `approvalId`, policy version and observation
reference against trusted storage. A language model or unauthenticated HTTP body
must never be that authority. Part 1 has no live HTTP admission endpoint.

Pending operations must be explicit null or an unresolved operation in
`prepared`, `withdrawing`, `in_transit`, `depositing`, `unknown` or `failed` state.
All unresolved states block another move, including retries with the same ID.
Recovery must verify/settle an operation before clearing it. Missing pending
state is blocked, never interpreted as no pending transfer.

Static configuration contains only Ethereum Sepolia (`11155111`), Base Sepolia
(`84532`), logical pool slots and the logical managed asset. It contains no
user-specific policy, amount or wallet. Chain IDs are **not** CCIP selectors.
Real addresses, uint64 selectors (transported as decimal strings), routers,
forwarders and lane/token support remain later verified deployment inputs.

## Financial model and rounding

Annual percentage rate (APR) is simple annual interest, represented in basis
points: 400 is 4%, 700 is 7%, and 10,000 is 100%. A year is exactly
365 days = 31,536,000 seconds. The approved deadline is absolute. Each evaluation
calculates `remaining = max(deadline - injected_now, 0)`; it never renews a horizon.

For an amount `A` in integer token units, token decimals `D`, a token price `P`
in valuation microunits, rate difference `B` in basis points, and `T` remaining
seconds:

```text
additional interest = floor(A × P × B × T / (10^D × 10,000 × 31,536,000))
incremental cost = upfront moving costs + difference in eventual exit costs
net improvement = additional interest − incremental cost
```

Multiply before dividing; do not round the principal valuation first. Positive
earnings round down. Negative additional interest rounds toward negative infinity
so losses are not understated. Each upfront fee line rounds up to a valuation
microunit, then lines are summed.

Seven explicit, disjoint upfront categories are required exactly once:

| Category | Responsibility |
| --- | --- |
| `withdrawalFee` | Source pool/protocol fee, excluding transaction gas |
| `sourceApprovalGas` | Required source token approvals, excluding other source gas |
| `destinationApprovalGas` | Required destination token approvals, excluding other destination gas |
| `sourceGas` | Source withdrawal/send transaction gas, excluding approval gas |
| `bridgeFee` | Quoted CCIP/transport charge, including any bundled destination execution |
| `destinationGas` | Extra destination execution gas not already included in the bridge quote |
| `depositFee` | Destination pool/protocol deposit fee, excluding gas |

Each line records amount, fee asset, chain, asset decimals, price in the **same
valuation unit**, observation time, source and simulation status. Quotes for the
same chain/asset must agree on price/decimals. Managed-token fees must match its
valuation. Explicit zero quotes are required for inapplicable/bundled categories;
their source must explain what is zero or where the charge is included. Trusted
quote adapters are responsible for that fee partition; the evaluator cannot
detect two descriptions of the same underlying provider charge.

`exitIfStay` and `exitIfMove` cover total eventual withdrawal/gas costs under each
alternative, as rational valuation microunits. Their **exact difference** is
rounded up only after subtraction, so equal common costs cancel even when
fractional. A negative difference represents exit savings, rounded conservatively
toward zero. Separate rounded exit figures in strategy accounting are display
estimates; the calculation uses the original exact quotes.

Initial deposit costs, realized withdrawal costs and prior rebalance costs remain
separate strategy-accounting fields. They are already incurred and do not change
whether this new move beats staying. The comparison subtracts only the extra
exit cost, not both exit costs or common initial fees.

The fee cap uses `upfront + max(exit difference, 0)`, so a possible later exit
saving cannot hide excessive immediate fees. Net improvement must be strictly
positive **and** at least the user's inclusive minimum. An equal fee cap is
permitted. Expiry, cooldown, already being in the destination and insufficient
economic benefit produce `hold`; missing/inconsistent data and failed safety or
funding limits produce `blocked`. Both include structured reasons.

## Balance, reserve and observation rules

The movable position is invested principal. The reserve is a **separate, idle
managed-token balance on the source chain**, not part of the deposit. It must
cover the protected amount plus existing commitments. An invested balance does
not satisfy the idle-reserve requirement.

The model deposits the full movable amount. Fees must be funded separately from
unreserved idle fee balances, grouped and summed by chain/asset. It does not
silently deduct bridge/token fees from invested principal. If fees are paid in
the managed source token, its fee balance must equal the observed idle reserve
balance and its reservations must cover the protected reserve and commitments.
Only a surplus above them is spendable. Native gas balances are separate from
token balances. Unknown fee funding blocks the move.

The source pool must be able to **withdraw** the amount. The destination must
have **deposit capacity** for it. No destination token-balance minimum is used:
a new deposit brings its own tokens. Policy approval is checked against complete
chain/pool/token tuples, not independent allowlists that permit new combinations.

All position, reserve, APR/liquidity/capacity, price, fee-funding and fee/exit-quote
observations require a timestamp and provenance. Future observations, stale
observations and excessive skew between observations are blocked. Simulation
status must be permitted by policy. Plan approval and prior move completion
timestamps also cannot be in the future. Unknown/malformed numbers and missing
required fields return blocked results, rather than default rates or zero fees.

The initial model assumes unchanged APR and **zero transfer downtime**. Every
calculation labels these assumptions and its estimate/simulation status. A
nonzero downtime input is rejected by this model version. Later parts must extend
the model explicitly and record actual in-transit time rather than imply these
fixtures already price that risk.

## Labelled example scenarios

These are **arithmetic fixtures, not required testnet balances**. No live RPC or
price feed is queried. CCIP-BnM has no reliable market dollar price. Here only,
one test token is defined as one **fictional simulated dollar**, unit `SIM_USD`,
with six decimal places. Every fixture rate, balance, capacity, price and fee
carries `simulated: true` and an `arithmetic-fixture:` source. The fee asset is
also fictional and separately funded. No actual ETH or LINK price is implied.

Both examples move 800 invested tokens, preserve 200 separate idle tokens,
compare 4% versus 7% APR, and charge 2 simulated dollars in incremental fees.
All other explicit fee/exit categories are zero. Their frozen clock is
2026-11-06 12:00:00 UTC, independent of the machine clock.

| Remaining time | Additional interest, micro-SIM_USD | Incremental cost | Net improvement | Decision |
| --- | ---: | ---: | ---: | --- |
| 60 days | 3,945,205 | 2,000,000 | 1,945,205 | Move |
| 30 days | 1,972,602 | 2,000,000 | −27,398 | Hold |

With 60 days remaining, the extra 3 percentage points of annual interest produce
about 3.945205 simulated dollars. After the 2-dollar moving cost, the estimate is
1.945205 dollars better than staying and meets the 1-dollar fixture minimum.
With only 30 days remaining, the extra interest is 1.972602 dollars. Moving would
leave the user 0.027398 dollars worse off, so the module holds. Neither example
invests the reserve or projects earnings on a combined 1,000-token position.

## Sources, skills and inspected upstream version

Official sources read on 2026-11-06:

- [CRE overview](https://docs.chain.link/cre/overview).
- [Multi-chain token manager documentation](https://docs.chain.link/cre-templates/multi-chain-token-manager).
- [Template source, pinned commit](https://github.com/smartcontractkit/cre-templates/tree/d0223f31182c76bc36b1cc9d47b13b18efcf2bf6/starter-templates/multi-chain-token-manager).
- [Developer agent skills documentation](https://docs.chain.link/resources/chainlink-developer-agent-skills).
- [Official skills repository, pinned commit](https://github.com/smartcontractkit/chainlink-agent-skills/tree/1d410d1b921ef49f79bbd04e57a85b21bf726b63).
- [TypeScript/QuickJS runtime](https://docs.chain.link/cre/concepts/typescript-wasm-runtime).
- [Current CCIP tooling index](https://docs.chain.link/ccip/tools/llms.txt).

The requested skills were already installed at project level under
`.agents/skills/` with official provenance in `skills-lock.json`. Read both full
skill files and checked their official repository files: `chainlink-cre-skill`
0.0.31 and `chainlink-ccip-skill` 0.0.14. Relevant references read: CRE concepts,
domain patterns and SDK reference; CCIP monitoring. Their guidance informs the
deterministic module, decimal-string transport, provenance, selector boundary and
later recovery responsibilities. No new CRE workflow, Solidity contract or
execution path is requested in this part, so workflow scaffolding/deployment and
Solidity generation are deliberately outside the current change. Available
skills were inspected for fit; no separate general backend/testing skill was
needed for this isolated module. No global instructions were changed.

The template was shallow-cloned for read-only inspection outside the project at
`/tmp/notsureyet-part1-cre-templates`. Its actual TypeScript orchestration, entry
point and tests, plus `MockPool.sol`, `ProtocolSmartWallet.sol`, interfaces,
manifests and contract lock/config were inspected. GitHub's tree page failed in
the browsing tool; Git clone resolved it. The runtime `.md` URL failed through
browsing and returned HTTP 403 through Python; its normal official page worked.

Important differences from the template:

- Its `shouldRebalance` compares an APR gap with `minBPSDeltaForRebalance`, without
  a deadline, position value, incremental fee, reserve or per-user policy check.
- It reads RAY-scaled APR and converts APR/APY display values to floating point.
  Our economic calculation stays in integer basis points and simple interest.
- It reads pooled balances and submits a cross-chain withdrawal/deposit report;
  this part performs no such effects. Its post-send balance increments describe
  intended outcomes, not proof of finalized destination deposits.
- The mock pool tracks deposits/withdrawals but does not accrue real interest.
  Simulated APR therefore does not prove earned testnet interest.
- The inspected smart wallet uses older extraArgs, exposes a public withdrawal/
  bridge method and handles received token entry zero. Do not copy it as a ready
  execution/security boundary; later parts require separately reviewed admission,
  all-token accounting, route checks and recovery appropriate to current CCIP.

### Versions

| Component | Observed/pinned version |
| --- | --- |
| Template commit | `d0223f31182c76bc36b1cc9d47b13b18efcf2bf6` |
| Template TS CRE SDK requirement | `^1.6.0` (range, not an installed/resolved version) |
| Template TS viem / Zod / TypeScript | `2.34.0` / `3.25.76` / `5.9.3` |
| Template Solidity / CCIP / Chainlink contracts | `0.8.26` / `contracts-ccip-v1.6.0` / `contracts-v1.4.0` |
| Template OpenZeppelin / forge-std | `v5.0.2` / `v1.11.0` |
| Local Node / npm / CRE CLI | `v22.14.0` / `10.9.2` / `v1.39.0` |
| New module TypeScript / Zod | exactly `5.9.3` / `4.6.7`, locked by npm |
| Existing Arkiv SDK / resolved viem | `1.0.1` / `2.57.16`, preserved |
| Existing CCIP / Chainlink Local / Chainlink contracts | `2.0.0` / `0.3.1` / `1.5.0`, preserved |
| Existing Foundry packages | `1.7.1`, preserved |

Zod was initially matched to the template, then updated before delivery after
`npm audit` reported an issue affecting versions below 4.6.6. The module pins
4.6.7. See the [reviewed advisory](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5).
The new module does not depend on the CRE SDK yet. It uses standard JavaScript
and basic validation; actual QuickJS/WASM dependency compatibility still needs
Part 2's simulation. A successful Node build is not proof of WASM execution.

## Commands and verification record

From the repository root:

```sh
npm ci
npm run lending:build
npm run lending:test
npm run lending:examples
npm test
AGENT_RUN_LIVE_TESTS=0 uv run pytest -q
npm audit
git diff --check
```

`lending:examples` emits the full labelled input and structured bound result for
each scenario as JSON records. `lending:test` builds first, then runs frozen-clock
Node tests. `npm test` includes both existing JavaScript tests and the new suite.
Generated JavaScript/declarations go in ignored `.local/lending-build`; no emitted
code needs committing. Financial logic exists only in TypeScript, not in a Python
translation or a second CRE-specific implementation.

During setup the default npm cache returned ENOENT. Retrying with an isolated
cache at `/tmp/notsureyet-part1-npm-cache` succeeded; no cache contents or secrets
were inspected. Reproduction can use `npm --cache /tmp/notsureyet-part1-npm-cache ci`
if that local cache issue persists. Added dependencies are exactly TypeScript
5.9.3 and Zod 4.6.7; the lock retains existing resolved dependency versions.

Completed verification on 2026-11-06:

| Check actually run | Result |
| --- | --- |
| `npm --cache /tmp/notsureyet-part1-npm-cache ci` | Clean lockfile install succeeded; zero audit vulnerabilities |
| `npm run lending:test` | Strict TypeScript build and 35 focused frozen-clock tests passed |
| `npm run lending:examples` | Both examples ran and returned the exact required move/hold microunit amounts |
| `npm test` | 19 existing JavaScript tests plus 35 lending tests passed |
| `AGENT_RUN_LIVE_TESTS=0 uv run pytest -q` | 185 passed; 2 paid live-inference tests intentionally skipped; one existing `websockets.legacy` deprecation warning |
| `npm --cache /tmp/notsureyet-part1-npm-cache audit --json` | Zero reported vulnerabilities after updating Zod |
| `git diff --check` | Passed |

Focused coverage includes all requested arithmetic and safety cases, minimum
benefit/fee/cooldown boundaries, every observation's freshness, integer precision
above JavaScript's safe-integer range, conservative cost rounding, common exit
cost cancellation, separate sunk costs, fee balance aggregation, frozen run/result
objects and isolation between two users. An intermediate malformed-APR test
caught an unsafe validation refinement; chained validation now rejects malformed
integers before conversion, and that regression passes.

No Solidity behavior changed, so no additional contract deployment/build/test was
required. These results do not claim live wallet, messaging, lending pool or CRE
execution verification.

## Blockers and limits before later parts

There is no missing prerequisite for Part 1's local decision/test deliverable.
CRE CLI presence/version and `cre workflow simulate --help` were checked. The
installed CLI supports explicit `--target`, `--non-interactive`, `--trigger-index`
and `--http-payload`; broadcast defaults to false. No workflow simulation was
run, and no login/authentication state was inferred. A runnable workflow is Part
2; this part neither installs the SDK nor claims a successful CRE dry run.

Later parts must resolve and verify actual testnet token/pool/consumer addresses,
selectors, allowed lanes, authenticated observations and fee quotes; configure a
receiver-free dry-run workflow; and validate the pinned dependencies in WASM.
Execution additionally needs atomic operation admission, wallet authority,
reconciliation of source/destination effects and accounting for actual downtime.
WhatsApp scheduling/admission and live recipient experience remain Part 4.
