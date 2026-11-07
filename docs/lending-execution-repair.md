# CRE execution failure investigation — 7 November 2026

The approved plan reached its initial deposit and calculated a profitable move.
Its execution workflow then failed before sending a transaction. This was an
implementation bug, independent of whether the pool rates were simulated.

## Confirmed cause

The real CRE CLI rejected the saved operation with:

```text
LimitExceeded: capability call limit exceeded for evm.CallContract:
PerWorkflow.ChainRead.CallLimit ... cannot use 16, limit is 15
```

The complete path attempted 21 reads: twelve historical contract/block reads
plus nine current-state contract reads. The financial evaluation ran by itself
within the limit; execution repeated extra checks and exceeded it. The SDK test
harness did not enforce that quota, so those tests passed while the actual CLI
failed. Chainlink documents the [15-read limit](https://docs.chain.link/cre/service-quotas).

The application marked submission uncertain before launching CRE, discarded
stdout/stderr, and ignored the exit code. A failure before the send therefore
looked identical to a lost response after a send. Its duplicate-transfer guard
then correctly refused another attempt until the old instruction expired and
the source chain proved that nothing had been sent.

There were also misleading status messages. A pending rebalance set
`management_active` false even after a verified initial deposit. A temporary
network read failure could prevent status from showing saved progress, and the
old generic error survived later successful monitoring.

## Changes

- Current-state reads use Multicall3, one batch on each testnet. All existing
  balance, rate, policy, fee, pending-operation and replay checks remain. Total
  CRE reads are fourteen, including block headers; default limits stay enabled.
- The `execution-preflight` target checks the complete input and report metadata
  without calling the chain-write capability. The application also simulates
  the strategy consumer directly, because an outer forwarder success alone
  does not prove that the inner strategy action succeeded.
- Failures before broadcast cancel the prepared operation with a specific
  diagnostic. They do not create an uncertain submission. After broadcast
  begins, missing results still require reconciliation, never blind retries.
- Compact CRE result logs preserve source transaction hashes, including hashes
  of reverted transactions. CLI failures retain safe diagnostic codes. A
  simulation result is never recorded as an on-chain transaction.
- Progress keeps the plan active while a rebalance is pending. Status labels
  cached observations when refreshing fails and preserves the approved plan
  and saved receipts. Successful monitoring clears a recovered check error.

The change does not replace the wallet, redeploy contracts, modify the approved
amount/reserve/deadline/fees/cooldown, or reset any transaction journal.

## Verification

The execution harness now enforces the actual fifteen-read limit, counting
headers as well as contract calls. It also rejects failed batch subcalls,
changed rates/fees/policies, and missing transaction hashes. Application tests
cover preflight cancellation, inner consumer failures, source-hash persistence,
lost responses, duplicate prevention, and accurate status after RPC failure.

The real CRE simulator reproduced the original quota failure. After batching,
the same saved input passed the read checks and reached the expected expired
instruction guard. The receiver-free target compiled and ran using the pinned
Bun dependency. A separate native CRE demo changed a controlled pool rate from
7% to 20%, calculated the benefit after fees, completed the mock-ledger move,
and verified withdrawal in that isolated fixture.

Local diagnostic artifacts are in `.local/manager-root-cause/`. They are ignored
by Git. The live application is being checked separately against the original
approved plan and its public-chain receipts; fixture results are not a claim of
testnet delivery.

The intended strategy follows the
[Chainlink multi-chain token manager template](https://docs.chain.link/cre-templates/multi-chain-token-manager):
observe controlled pool rates, choose a beneficial destination, move test tokens
through CCIP, and deposit them into the destination pool. The shared evaluator
additionally applies the approved horizon, costs, reserves and cooldown.

## Resumed investigation and repair

The interrupted session had closed its first failed operation, but its next
attempt also stopped before submitting a transaction. The resumed investigation
identified two more independent defects:

1. CRE loads `.env` and `.env.public` with override semantics. Those files replaced
   the signer supplied by the application. The native broadcast exited during
   initialization with `a private key is required for --broadcast mode`. The
   broker now selects `/dev/null` for both files so its supplied signer cannot be
   replaced. No key was printed or written to disk. This specific early rejection
   can close a journal claim with recorded proof of no submission. Timeouts and
   other uncertain results still require onchain reconciliation.
2. The real mock forwarder passes 64 bytes of metadata. The existing deployed
   wallets reject any nonempty mock metadata. Transaction
   `0x65429c799a6200dbc6866c7453448f04cc4f546ec6f2e91f1132922a837fba4c`
   therefore emitted `ReportProcessed(..., false)` without `SourceSubmitted`.
   Historical calls at block 12080118 reproduced `Unauthorized` with the real
   metadata and successful execution through the same forwarder's public `route`
   method with empty metadata and the same signed body.

The application now runs native CRE validation and report generation using
`cre workflow simulate part3 --target execution-preflight --non-interactive --trigger-index 0 --http-payload <saved-operation.json>`.
It then simulates and signs the compatibility `route` call to the existing mock
forwarder. The wallet still verifies the owner signature, amount, destination,
reserve, policy, expiry, fees and replay protection. The application journals the
raw transaction and exact hash before sending. This is an application relay of a
CRE-validated report, not a native `writeReport` broadcast or DON deployment.
No replacement pool, wallet or approval is introduced.

A subsequent real route transaction exposed a separate gas-estimation defect.
Transaction `0xa755006d3c8cedf3871d42b43281e792342bf613272716fb5d5c6d6eb75441d2`
had an estimated/padded gas limit of 1,070,604, returned outer success, and emitted
no logs. Replaying its exact calldata at the preceding block returned `false`
with that limit and `true` with the already validated 1,500,000 limit. Because the
forwarder catches receiver failures, ordinary `estimateGas` cannot distinguish
these outcomes. Report transactions now use the explicit 1,500,000 limit from
their CRE cost budget and preflight, and simulate once more with the exact gas
limit and gas price before signing. Native fee caps still apply. Historical
evidence is in `.local/manager-root-cause/forwarder-gas-proof.json`.

The polling loop now backs off after failures and remembers its last warning
independently of current health. An intermittent successful read can no longer
cause the same warning again seconds later. Successful idle monitoring also polls
less frequently. RPC requests are batched and connection failures retain a safe
diagnosis without exposing provider URLs.

The resumed native CRE/mock-ledger conversation completed deposit, rate-driven
rebalance and withdrawal. Its output is
`.local/manager-root-cause/resumed-native-demo.log`. The historical forwarder proof
is `forwarder-compatibility-proof.json` in the same directory. These simulation
artifacts are distinct from live delivery receipts.

The user subsequently requested removal of the remaining finality wait for
WhatsApp testing. The dedicated Sepolia/Base Sepolia application now uses
canonical mined receipts for both transfer endpoints and expired no-send proofs.
The standalone Part 3 tools retain finalized confirmation by default. Receipt,
event, token/amount, block-hash, expiry and pending-nonce checks remain mandatory.
This explicitly accepts testnet reorganisation risk. The existing deployed
wallet's `_message` still encodes block depth zero, which `FinalityCodec` defines
as full finality. Removing the application's waits does not alter that contract
or a message already submitted. Future faster CCIP transfers would need a
different contract configuration supported by the token pool and verifier.

## Live source transfer

The corrected relay emitted `SourceSubmitted` for exactly 0.8 CCIP-BnM at Sepolia
block 12080199. Source transaction:
`0xfe22bab12ef932d7b616ffde62c99743b06afe4afe5bea2f5e44106c7a71feb8`.
CCIP message:
`0xcde8280c90e4643f766d4b532d3cb8695c4fed7a801d953c44fe65b6211e9ab9`.
The live CCIP API independently matches the source wallet, destination wallet,
both token addresses, chain selectors, amount, and source transaction. At the
initial check it reported `SENT`, `AWAITING_VERIFICATION`, and
`finalityType: FINALIZED`; destination deposit is not yet established by that
source receipt. The WhatsApp channel accepted the update containing this new
message's explorer link.

## Verified live completion

Base Sepolia transaction
`0xcf51dce6b0758bf5f71b768e83783ba94b79a206db9388449cc1e3d7efda03e9`
at block 55458413 emitted `PoolDeposited` for that same operation and CCIP
message. Verification checked the exact 0.8 token transfer from the strategy
wallet into the approved pool, the event's `principalAfter`, and the pool's
balance at the receipt block. The operation is `verified_deposit`; the final
Base principal is 0.8 CCIP-BnM, with 0.2 idle reserve on each chain. CCIP reports
`SUCCESS` and `EXECUTED`. Actual transit was 1,143 seconds, including the full
finality requested by the already deployed wallet. The application saved the
completion time for the original policy's cooldown and remains active.

The completion record is `.local/manager-root-cause/live-completion.json`.
The native CRE/mock-ledger deposit, rebalance, and withdrawal simulation also
passed independently. These are controlled mock lending pools: their APR is
simulated and they do not accrue real interest.
