# Part 3 — execution, monitoring and recovery

Updated 2026-11-06. **The integration and local tests are implemented. Live
acceptance remains incomplete. No Part 3 source transaction or CCIP message has
been submitted, and no live destination deposit has been verified.**

## What this changes

Part 3 adds a path from an approved financial decision to an actual token send,
then checks what happened on both chains. It keeps the existing decision engine
as the only financial calculation. It does not change Part 2's workflow, wallet
contracts, setup state or deployment manifest. It does not connect WhatsApp.

The existing Part 2 wallets deliberately disable cross-chain execution. They
are not upgradeable. A separate `ExecutionStrategyWallet` is therefore required
before a live test can run. The new deployment script and setup-artifact builder
are user-run handoffs. They do not deploy anything automatically.

The two permitted networks remain Ethereum Sepolia and Base Sepolia. The only
managed token is CCIP-BnM. These mock pools account for principal and do not
accrue interest. All fictional valuations and controlled rate assumptions remain
labelled in the approved input.

## Data flow and authorization

1. `scripts/part3/cli.mjs prepare` reads both finalized testnet states. It checks
   the direct lane and exact remote token using the actual routers and token
   registries. It compares deployed runtime code with the compiled Part 3 wallet
   and mock pool. Compiler-declared immutable slots are masked only for this
   code comparison; the separate contract binding checks their actual values.
2. The adapter refreshes principal, reserve, rates, capacity, fee balances and
   the router fee for the exact operation ID and amount. It reuses the existing
   financial evaluator. A hold or blocked result writes `decision.json` and
   creates no executable instruction.
3. A move produces an exact EIP-712 instruction and a public review artifact.
   EIP-712 is the typed message format MetaMask displays and signs. The signature
   covers the chain, wallet, fixed peer, token, amount, policy version, expiry,
   LINK limit, gas-price limit and a hash of the entire observation/policy input.
   The signer helper uses the same injected MetaMask interface as the existing
   setup UI. It downloads the signed artifact and never sends a transaction.
4. `admit` verifies that signature, refreshes state again and persists the exact
   signed operation. `claim` repeats the checks, records owner nonce information
   and durably enters `submission_unknown` before providing the execution
   command. A second process cannot claim it or prepare another unresolved
   operation for the same owner.
5. The user runs the CRE command. The separate Part 3 workflow verifies the
   immutable block/hash evidence through Part 2's existing adapter, then reads
   current finalized state again. State or quote drift prevents a report write.
   A valid move becomes a raw ABI report through `runtime.report` and
   `EVMClient.writeReport`.
6. The wallet authenticates the forwarder and owner signature, rejects expired
   or replayed instructions, checks its actual pool balance, rate, reserve,
   actual LINK quote/balance and transaction gas price, then withdraws and sends
   the full managed principal. The permanent peer and active-operation guard
   prevent an unrestricted recipient or a concurrent second send.
7. The destination authenticates the CCIP router and the exact source-selector /
   sender pair. It accounts for every token entry and the complete amount. It
   attempts the pool deposit through a self-call so a deposit error can be
   recorded without losing track of delivered tokens.
8. The monitor verifies finalized source and destination receipts and the final
   lending position. Only this evidence can produce `verified_deposit`.

The standalone SQLite journal is a durable **single-host execution cache** for
this user-run test flow. It uses WAL mode, full synchronization and transactions
with a unique unresolved-owner index. Keep the database and its WAL together
across restarts; never delete them to retry. The owner's exact signature is the
approval authority here. This does not introduce a second application task
authority or add Arkiv writes. A future distributed application admission path
must integrate with the existing Arkiv/checkpoint boundary before enabling more
than one independent host. That is not enabled by this CLI.

## CRE simulator authentication limitation

The native simulator was actually exercised. Its report preview returned a
fixed mock workflow ID, mock owner and the encoded workflow name `eb6597b43f`.
This name is the first ten ASCII hexadecimal characters of SHA256(`lending-p3`),
not the readable name itself. The official
[consumer guide](https://docs.chain.link/cre/guides/workflow/using-evm-client/onchain-write/building-consumer-contracts)
also states that `MockKeystoneForwarder` does not supply authenticated workflow
metadata. This does **not** mean the metadata is empty: the deployed Sepolia
mock forwarder passes 64 bytes from the raw report header.

Accordingly, the contract recognizes only the two explicitly configured,
verified testnet mock-forwarder addresses for the simulator path, accepts their
empty metadata and **still requires the independent owner signature over the
exact action**. Arbitrary nonempty mock metadata is rejected. The separate
non-mock path checks the workflow ID, correctly encoded name and owner. There
is no production deployment or claim that the mock path proves DON consensus.
DON means decentralized oracle network. Consequently, direct `writeReport`
delivery is incompatible with the existing empty-metadata wallets. Part 4 uses
native CRE `execution-preflight`, then relays the same signed body through the
mock forwarder's public `route` method with empty metadata. This preserves the
existing contracts and all owner-signature checks. The actual deployed forwarder
was checked with historical `eth_call`, not just a simplified contract fixture.

This is a limitation of simulation, not a reason to treat public mock metadata
as authenticated identity or to remove owner authorization. The native preview
does not itself prove the deployed forwarder's callback behavior; that remains
part of the pending live consumer acceptance check.

## Fees and balance accounting

The actual `quoteOperation(operationId, amount)` call constructs the same
message as execution. It uses the installed CCIP 2.0 extraArgs V3 helper, requests
full finality and budgets 500,000 destination callback gas. Installed contracts
encode a zero block depth as full finality; no Fast Transfers are enabled.

The LINK bridge quote includes destination execution and its approvals.
`destinationGas` and `destinationApprovalGas` must therefore be explicit zeros.
The mock pool charges no protocol fee. Source token/router approvals happen
inside the report consumer and are part of `sourceGas`; counting them again as
`sourceApprovalGas` is rejected.

Source gas has a separate conservative budget of 1,500,000 gas at twice the
observed source gas price. This is an estimate, labelled as such. The signature
binds the corresponding gas-price ceiling, and the wallet checks `tx.gasprice`
before moving principal. A cost-policy or funding rejection prevents the
workflow's source write. Gas can still be spent by a failed external transaction
or by an operator choosing different transaction settings. The installed SDK's
write interface exposes a gas limit but no separate maximum gas-price setting;
the user must also bound the broadcast transaction to the reviewed budget.

The full principal is sent. LINK and native gas are separately funded. The
monitor never credits predicted destination principal and never counts tokens
in transit as an additional deposit. Source and destination block timestamps
record actual transit time separately from the existing evaluator's explicitly
labelled zero-downtime assumption.

## States and recovery

| State | Meaning / next step |
| --- | --- |
| `prepared` | Signed input persisted; no submission claim has started |
| `submission_unknown` | Submission may have happened; reconcile events, hash and nonce information |
| `submitted` | A source hash is recorded; consumer execution is still unproven |
| `source_confirmed` | Finalized matching `SourceSubmitted` event includes the actual message ID |
| `in_transit` | Source confirmed; destination evidence remains pending |
| `destination_failed` | CCIP reports failed execution; inspect the existing message |
| `destination_executed` | Destination receipt exists; pool deposit is not yet verified |
| `idle_tokens` | Delivery succeeded but the caught pool error left the exact tokens idle |
| `verified_deposit` | Matching wallet/token/amount, pool transfer and position increase verified |
| `failed` | Source attempt failed, including an outer success with no inner consumer event |
| `cancelled` | Unclaimed proposal cancelled, or finalized expiry/no-send proof established |
| `recovered_idle` | Failed deposit's exact tokens returned to the fixed owner; not a completed lending move |

A timeout, unindexed message, missing transaction receipt or API outage remains
pending. Unknown API status values are retained without asserting success.
Source event searches use bounded finalized block ranges and persist progress.

For an uncertain source submission, search for the existing operation's event
and message ID. Never blindly run a fresh transfer. `resolve-no-send` can clear
the lock only after the signed report has expired on the finalized source
chain, its sent-message and active-operation slots are zero, owner nonces have
no unresolved pending transaction, and any known source receipt is finalized
without a matching send. The immutable operation ID remains recorded forever.

For a failed CCIP execution, `recovery` checks current failure and eligibility
before offering the supported CCIP Explorer recovery for that **same message**.
It does not generate a replacement source send. See
[manual execution](https://docs.chain.link/ccip/concepts/manual-execution).

For idle delivered tokens, `recovery` produces owner-only `retryDeposit` calldata
for the existing message. Reserved inbound tokens cannot be spent through the
ordinary idle-withdrawal/deposit methods. The contract retains the entire
message, every token entry, failure reason and resolution state. An alternative
owner-only recovery returns the exact tokens to that fixed owner. Unknown or
already-resolved messages cannot be retried or recovered twice.

Attach a known recovery hash using `attach-recovery`. If that response was lost,
the monitor searches finalized destination events for the original message ID
and verifies the discovered receipt. A restart does not require another send.

`settleOperation` is a separate source-owner acknowledgement after independently
verified completion. It only releases the source admission guard. It is not
destination evidence and cannot mark the journal complete. Until it is signed,
the source contract deliberately blocks the next move. The `settle` CLI command
prepares its exact owner-signed transaction only for a `verified_deposit` record.

## Reproducible commands

From the repository root:

```sh
npm run lending:part3:test
node scripts/part3/native-check.mjs
npm run lending:part3 -- route
npm run lending:part3 -- inspect-existing .local/lending-metamask/manifest.json
```

The native check runs these exact **non-broadcast** commands from `cre-lending`:

```sh
cre workflow simulate part3 --target local-simulation --non-interactive --trigger-index 0 --http-payload '{}'
cre workflow simulate part3 --target staging-settings --config config.report-preview.json --non-interactive --trigger-index 0 --http-payload ../tests/helpers/part3-report-vector.json
```

The first has an early return with no reads, reports or writes. The second
verifies the complete Part 3 ABI encoding against the independent public vector,
checks the native report body and previews metadata. It never submits a report.
Neither proves token delivery.

For a **completed public plan** using the existing Part 2 plan schema:

```sh
npm run lending:setup:build
node scripts/part3/setup-artifacts.mjs .local/completed-plan.json .local/part3-setup-review .local/lending-metamask/manifest.json
```

`setup-artifacts` preserves the supplied policy deadline, amount, reserve and
limits. It changes the reviewed deployment script to the Part 3 implementation
and records current route evidence. It reuses the confirmed owned pools from the
existing manifest, checks their code/owner/token and proposes only new execution
wallets. The returned deployment review contains the exact per-chain constructor
tuple and method. The caller signs deployment and
configuration only through their own wallet-controlled integration. New wallet
addresses and receipts must be confirmed before replacing the null manifest
fields. Do not start a second managed position alongside the old Part 2 test:
complete Part 2 first, then explicitly review any principal migration and its
setup costs. No migration is performed by this implementation.

After setup, use the **separate** confirmed manifest and the generated seed.
The Part 3 `observe` command creates a fresh finalized observation without
depending on Part 2's ongoing runner. `prepare` refreshes that snapshot again,
checks actual Part 3 runtime code and quotes the exact message. A Part 2 wallet
is rejected before an executable action is prepared.

```sh
npm run lending:part3 -- observe .local/part3-setup-review/manifest.json .local/part3-setup-review/seed-run.json .local/part3-observation.json
npm run lending:part3 -- prepare .local/part3-observation.json .local/part3-review
npm run lending:part3:sign -- .local/part3-review
```

Open the printed loopback URL in the MetaMask browser, review and sign. Place the
downloaded `signed.json` in that review directory. The signature does not send
funds. Admission and submission are separate:

```sh
npm run lending:part3 -- admit .local/part3-operations.sqlite .local/part3-review/signed.json
```

The admitted record contains the exact operation ID. Use that ID with `claim`,
`attach`, `monitor`, `watch`, `recovery`, `attach-recovery` or `resolve-no-send`.
The CLI prints complete operation-specific commands and a review block rather
than guessing an ID or address. `claim` takes a new payload output path; it
persists the submission claim before printing the execution handoff.

For the review directory above, read the exact ID from the signed artifact and
claim it. This command reads public approval data, not wallet credentials:

```sh
part3_operation_id=$(node --input-type=module -e "import {readFileSync} from 'node:fs'; console.log(JSON.parse(readFileSync('.local/part3-review/signed.json','utf8')).instruction.operationId)")
npm run lending:part3 -- claim .local/part3-operations.sqlite "$part3_operation_id" .local/part3-review/claimed-payload.json
cd cre-lending
cre workflow simulate part3 --target staging-settings --non-interactive --trigger-index 0 --http-payload ../.local/part3-review/claimed-payload.json
```

Do not add `--broadcast` for the existing empty-metadata wallets: the forwarder
accepts that transaction but its inner consumer rejects the metadata. Use the
approved Part 4 application relay described above for those deployments.
These commands require the confirmed setup and admitted signature above. The
actual `claim` handoff contains the resolved absolute path, exact chains, peer, token/amount, payload hash and
expected effects. The dedicated funded owner must be the broadcast account.
The agent never reads or handles its signing material.

`watch` polls with bounded backoff and resumes from SQLite after a process
restart. `status` shows reasons, hashes and recovery state. Back up the database
with a proper SQLite backup or while the process is stopped; do not copy only
the main database during active WAL writes.

## Evidence and remaining acceptance

The evidence directory is [evidence/part3](evidence/part3/test-results.json).
Local router fault injection and SDK mocks are labelled separately from native
CRE execution and live testnet reads. No local fork is represented as CCIP
delivery evidence.

| Check actually run | Result |
| --- | --- |
| Strict shared TypeScript build | Passed |
| Journal, receipt, signing-helper and encoding tests | 17 passed |
| Part 3 Solidity fault-injection tests | 13 passed |
| Part 3 CRE SDK harness | 9 passed; cost-cap and depleted-fee cases assert zero write calls |
| Native receiver-free CRE workflow | Compiled and ran without reads/reports/writes |
| Native full instruction ABI/report-body preview | Matched the independent golden vector; no report submitted |
| Existing JavaScript and financial decision tests | 19 + 35 passed |
| Existing Part 2 adapter/setup and CRE tests | 16 + 10 passed |
| Existing combined contract regression run | 34 passed at that run; the additional independent Part 3 vector test passed afterward |

The 39 focused Part 3 tests include process-level contention, a restart during
transit, unknown submission reconciliation, finalized expiry/no-send proof,
depleted fee funding, current rate/fee drift, destination failure, idle-token
recovery, lost recovery responses and detection of inner consumer failure.

The live read-only check of the existing deployment found 0.8 BnM invested and
0.2 BnM idle in the Sepolia Part 2 wallet, and zero invested with 0.2 BnM idle at
the Base Part 2 wallet. Its exact addresses, deployment receipts, finalized
blocks and funding balances are recorded in
[existing-deployment-check.json](evidence/part3/existing-deployment-check.json).
That is evidence of the existing Part 2 state, **not** a Part 3 transfer.

Still required for live acceptance:

1. Confirm the separately deployed Part 3 wallet pair and explicitly reviewed
   faucet-funded setup/migration after Part 2 is finished.
2. User-sign the exact instruction and run the user-controlled CRE broadcast.
3. Record source transaction hash, actual CCIP message ID and destination
   transaction hash. Verify the pool transfer and exact position increase.
4. Run a live cost-policy rejection, retaining evidence that no source write
   occurred. The automated `FEE_LIMIT` test already asserts zero write calls,
   but it is not live testnet acceptance.

The requested CCIP skill explicitly states: “Never deploy, register, bridge,
transfer, mint, burn, approve, or execute onchain manually.” See the exact
[SKILL.md](../.agents/skills/chainlink-ccip-skill/SKILL.md). Its signing boundary
is why live execution is left user-run. The complete implementation, test
evidence and handoff are provided without bypassing that restriction.

Official references checked for this work:
[overview](https://docs.chain.link/ccip/overview),
[tools index](https://docs.chain.link/ccip/tools/llms.txt),
[message lifecycle](https://docs.chain.link/ccip/concepts/message-lifecycle),
[test tokens](https://docs.chain.link/ccip/getting-started/test-tokens),
[BnM directory](https://docs.chain.link/ccip/directory/testnet/token/CCIP-BnM),
[Sepolia directory](https://docs.chain.link/ccip/directory/testnet/chain/ethereum-testnet-sepolia),
[Base Sepolia directory](https://docs.chain.link/ccip/directory/testnet/chain/ethereum-testnet-sepolia-base-1),
[CRE simulation](https://docs.chain.link/cre/guides/operations/simulating-workflows).
Installed CCIP 2.0.0, CRE SDK 1.23.0, viem 2.57.16 and Zod 4.6.7 are preserved;
no template-era CCIP 1.6 example or newer uninstalled SDK is mixed in.

This skill never signs or sends. You must sign and broadcast from your own wallet.
