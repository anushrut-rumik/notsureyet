# Part 4 — WhatsApp policy management and application scheduling

Updated 2026-11-07. The live funding, approved automatic execution and principal
withdrawal paths are implemented. The user selected their existing encrypted
agent wallet as owner. Local contract and fault-injection tests pass. **Live
acceptance is still pending:** the owner wallet is funded, but the new compatible
wallets have not been deployed and lending mode remains off. Real MetaMask funding
has been verified; live strategy deposit, cross-chain movement and withdrawal
have not yet been demonstrated.

## What a user can do now

With the explicit local mock, the user can propose a beginner or expert plan,
review its actual fixture balance and limits, approve that exact version from
the mocked WhatsApp sender, start and confirm an initial deposit, receive
scheduled hold/move results, track transit and destination confirmation, inspect
balances, pause and withdraw to their bound fixture wallet. Mock receipts are
local ledger records, never transaction hashes or evidence of a real deposit.

The same seven typed tools are registered with the existing Python agent:
`lending_portfolio`, `lending_markets`, `lending_create_plan`, `lending_start`,
`lending_pause`, `lending_withdraw`, and `lending_status`. Normal language can use
the existing model/tool loop. The deterministic command flow also runs without
model inference. No new inference provider, wallet provider or dashboard exists.
Live model selection of these tools has not been tested.

`PlanSettings` accepts integer token units as strings, a horizon in days and a
check interval. Beginner settings and expert settings pass through that schema,
then through the existing TypeScript policy schema. All financial decisions
still come from `lending/decision.ts`, through `evaluateRun`. Models cannot supply
observations, identity, wallet addresses, recipients, command lines, raw calldata,
approval state or arbitrary market/network lists to these tools.

## Exact local setup, start, test and stop

Run from the repository root. Use Node 22 or later (the Part 3 journal uses
Node SQLite), the repository's existing `uv` environment, and its pinned npm
dependencies. These commands neither install Composio nor use it.

```sh
npm ci
uv sync --group dev
npm run lending:build
npm run lending:part4:test
```

A self-contained scripted demonstration uses only mock HTTP/Redis transports and
a mock execution ledger. Its temporary directory contains a newly generated
fixture wallet, a private encrypted database and no real provider credentials.
The fixture explicitly provides 1,000 CCIP-BnM and 10 SIM_FEE_ASSET units per chain;
chat commands cannot mint or add funds.

```sh
npm run lending:part4:demo -- --scripted --evaluator test-harness
```

For interactive local use, leave off `--scripted`. `/quit` stops the worker and
removes the temporary fixture. Ctrl-C also stops the process.

```sh
npm run lending:part4:demo -- --evaluator test-harness
```

`test-harness` invokes the actual shared financial rules in Node, without the
CRE CLI. It is explicitly identified as such in status. To use the real CRE CLI,
use the existing CRE installation and account login. If it is not authenticated,
the user must complete `cre login`; no application credential is invented.

```sh
cre whoami
LENDING_NATIVE_TEST=1 uv run pytest tests/test_lending.py::test_native_cre_evaluation_opt_in -q
npm run lending:part4:demo -- --scripted --evaluator cre
```

Each evaluation invokes this command internally, with a private temporary input
file substituted for `INPUT_JSON`:

```sh
cd cre-lending
cre workflow simulate arithmetic-check --target local-simulation --non-interactive --trigger-index 0 --http-payload INPUT_JSON
```

No `--broadcast` is added to this arithmetic check. The adapter checks complete result chunks and compares
the CRE result with the same local rules at the CRE evaluation time. A missing
CLI, authentication failure, timeout, incomplete output or inconsistent result
prevents admission. This is an arithmetic-only simulation over the mock ledger,
not a deployed-data test or decentralized oracle network (DON) execution.

The application worker maintains the schedule. CRE simulation itself does not
keep the schedule running: a local cron simulation fires once. See the official
[simulation guide](https://docs.chain.link/cre/guides/operations/simulating-workflows)
and [consensus explanation](https://docs.chain.link/cre/concepts/consensus-computing).

## Commands and expert configuration

The existing provider command channel accepts:

```text
/lending plan
/lending plan {"amountUnits":"600000000000000000000","reserveUnits":"400000000000000000000","days":60,"checkIntervalSeconds":600,"maxFeeMicrounits":"2000000","minNetBenefitMicrounits":"500000"}
approve lending PLAN_ID VERSION REVIEW_DIGEST
/lending start
/lending markets
/lending status
/lending operation OPERATION_ID
/lending pause
/lending withdraw
```

The beginner example in the brief is recognized directly. Other natural-language
requests use the existing agent's tool loop. The beginner preset protects 20% of
the currently available source balance, proposes depositing the remainder,
checks hourly, caps each move at 3 fictional SIM_USD, requires at least 1 SIM_USD
of expected net improvement and uses a one-day cooldown. Every setting appears
before approval, including both approved pool identifiers, freshness limits and
the absolute UTC deadline. These are test suggestions, not investment guidance.

Approval is an exact incoming WhatsApp message. The stored event must belong to
the authenticated user and verified transport. The reply binds the plan ID,
version and review fingerprint. There is no approval tool and no `approved`
argument. Repeating an already processed message does not start another action.
Changing limits creates a fresh proposed version and requires fresh approval.
The deadline is fixed when the review is created; approval and later checks do
not extend it. A policy with an existing position must be withdrawn/reconciled
before replacement in this implementation.

## Scheduling, pause, expiry and withdrawal

The existing `Runtime.worker` polls lending independently of Arkiv task recovery.
Each active policy has a saved next-check time. Evaluations, commands, tracking,
pause and withdrawal share a per-user operating-system file lock. It works across
processes on the same host and releases automatically after process death.
Admission persists a prepared operation before submission. A second admission
rechecks the exact policy and evaluates fresh data before submission. Pause can
cancel between those two admissions. If submission wins the lock first, pause
tracks the submitted operation; it cannot undo it.

A stale prepared operation is cancelled after five minutes without submission.
Pending or unknown operations block new moves. Submitted mock transfers resume
tracking after a restart without being submitted again. Unknown outcomes remain
unresolved rather than being retried. Expiry stops new investments and cancels
prepared work, while submitted work remains tracked. The position stays
deposited until withdrawal is requested.

Withdrawal stops new investments, cancels prepared work and waits for submitted
transfers to settle. It then returns the available managed CCIP-BnM to the exact
verified owner, separately on each current testnet. The mock path reports success only after a labelled local ledger receipt.
The live path requires the exact signed transaction and canonical finalized
receipt, and confirms that principal and managed idle balances are zero. If pool
liquidity is insufficient, it withdraws the available portion and leaves the
remainder pending. It never calls the remaining principal withdrawn. Fee-token balances are reported
separately and are not part of this managed-token withdrawal. There is no bank
cash-out, bridge back to the source, borrowing or leverage.

The reserve remains idle on its original chain when principal moves. Live start funds the explicitly reviewed reserve separately on each chain.
The mock fixture keeps its original per-chain balances. No reserve is invented. A later move is blocked if its source-chain
reserve is insufficient. Status shows each chain's idle funds separately.

## Existing provider, identity and privacy

The integration reuses `IraWhatsApp`, the existing Redis stream consumer, the
sidecar bearer-authenticated outgoing HTTP client, allowed test-chat list,
persistent input deduplication, stable sender-LID identity mapping and delivery
journal. Outgoing routes are now rechecked against the current allowlist.
The authenticated sender selects the server-side user, which selects the
existing encrypted agent wallet or verified MetaMask connection. A watched
address cannot approve management. Lending commands/tools reject user-selected
identities passed through the local HTTP chat API.

This provider uses Redis intake, not a public webhook. No webhook endpoint is
added. The current compose Redis is loopback-bound but unauthenticated. Its
trusted producer and local-host boundary are assumptions, **not cryptographic
verification of every event**. The prebuilt `notsureyet-whatsapp:local` sidecar
source/version is not bundled, so a provider-event signature contract could not
be verified. Do not expose this Redis or the developer HTTP API publicly. A
public webhook deployment would require the actual provider's signature and
replay checks before admitting an event. That external step is unverified.

Policies and operation records reuse the existing `Store` database and `Vault`
encryption. They live in the private `lending_private` table, with full SQLite
synchronous durability. Nothing is written to public Arkiv. **This deliberately
changes the earlier proposed persistence boundary:** Arkiv remains authority for
existing background tasks, but the private local lending table is the authority
for these policies. It is not reconstructed from an Arkiv task checkpoint.
Back up the database/WAL consistently, the original vault key and relevant
wallet files together using the existing private backup process. Do not discard
the SQLite database as a cache or restore an old policy snapshot over newer
operation records. Cache-loss/remote recovery and multiple-host operation are
not implemented. No signing material is passed to the model, command arguments, logs or chat.
The fixed application broker unlocks the existing encrypted wallet and passes its
key privately to the fixed execution child through stdin. That child supplies it
only to the authorized CRE process environment. Public signed transactions and
policy instruction signatures are durable in private local journals. They are
not private keys.

## Live testnet setup and activation

The live path uses the same approved policy and financial rules as the mock path.
Each scheduled comparison invokes the real local CRE arithmetic workflow. A move
then invokes the existing Part 3 execution workflow with `--broadcast`; that
workflow independently checks the signed input, current observations and policy.
The application scheduler keeps it running. This is **local CRE execution**, not
a workflow deployed to a decentralized oracle network. The testnet forwarder
requires the bound owner's signed instruction, not trusted workflow metadata.

The agent may sign permitted actions only after the exact WhatsApp plan approval.
The review binds both wallet/pool deployments, owner, workflow, gas/funding limits
and a hash of the fictional valuation input. Changing those requires fresh
approval. The existing old MetaMask/Part 2 wallets are not upgraded or reused.

### One-time funding

The selected public owner is `0x29a73a629FFd5323dDcA4A474210d38d2722D41d`.
The read-only check on 2026-11-07 found 1 CCIP-BnM, 1.0475 test ETH and 25 LINK
on Ethereum Sepolia, and 0.2 CCIP-BnM, 0.005 test ETH and 25 LINK on Base Sepolia.
These balances can change; the setup and execution paths recheck them.
The initial funding targets are:

- Ethereum Sepolia: about 0.02 **test** ETH and 1 **test** LINK.
- Base Sepolia: about 0.02 **test** ETH, 1 **test** LINK and at least 0.2 CCIP-BnM.

Existing test ETH in MetaMask can fund this address directly. In WhatsApp, request
"Send 0.005 ETH on Base Sepolia from my connected MetaMask to my agent wallet."
Review and approve in chat, then confirm through the MetaMask link. The same flow
supports Ethereum Sepolia when explicitly requested. Each transfer stays on its
specified network. The default payment limit is 0.01 ETH per transfer; funding
one chain does not fund the other. LINK already exceeds the initial target above.

These are initial test budgets, not guaranteed lifetime funding. The prepared
strategy deposits 0.8 BnM on Sepolia and protects 0.2 BnM on each chain. Source mock
APR is 4%; destination mock APR is deliberately 10,000% to demonstrate a move with
small faucet balances. Neither pool pays interest. Pricing is fictional SIM_USD.
No mainnet tokens or pasted keys are required.

### Deploy the compatible wallets

From the repository root:

```sh
npm run lending:build
npm run lending:setup:build
uv run python -m agent.lending_setup
```

This is read-only and prints the review hash, chain IDs, predicted addresses and
six deployment/pairing actions. Inspect `.local/lending-agent-setup/review.json`.
Then run the same command with `--execute` and **the exact printed review hash**:

```sh
uv run python -m agent.lending_setup --execute REVIEW_HASH_FROM_PREVIOUS_COMMAND
```

The launcher uses the existing vault without exposing its key. It creates two
mock pools and two execution wallets and pairs the wallets. It does not transfer
BnM/LINK or deposit principal during setup. Each transaction is saved before
submission and reconciled by its exact hash. Keep the launcher running while it
waits for finality. Ctrl-C preserves the journal; rerun the identical command to
continue reconciliation. It never blindly resends an uncertain transaction.

Successful completion writes `.local/lending-agent-setup/bindings.json`. The
per-transaction native gas cap is 0.01 test ETH, and strategy LINK funding is
1 LINK on each chain. These values appear in the subsequent chat review.
Amounts/reserves are immutable deployment limits. Expert plans must match them;
changing those limits requires new reviewed deployments. Other policy settings
use the same validated expert schema. The live preset allows finalized data up
to 1,800 seconds old and interchain skew up to 900 seconds. More restrictive
settings can cause an explained hold.

Prepared on-chain action for user-run execution:
- Action: deploy and pair new agent-owned strategy wallets and mock pools.
- Network: testnet only, Ethereum Sepolia (11155111) and Base Sepolia (84532).
- Source chain: Ethereum Sepolia for the initial approved position.
- Destination chain: Base Sepolia for the paired destination.
- Route/lane: setup performs no cross-chain send; execution verifies the direct CCIP lane before every move.
- Token/amount: setup moves no BnM or LINK; later approved funding is 0.8 principal plus 0.2 reserve on Sepolia and 0.2 reserve on Base.
- Payload/contracts: exact CREATE bytecode, constructors, predicted addresses and peer calls in the hashed review.
- Method: two CREATE transactions and one `setPeer(address)` call on each chain.
- Expected effect: compatible wallets owned by the existing encrypted agent wallet; no active investment yet.
- User-run artifact: `agent.lending_setup`, using the exact emitted review hash.

Review this carefully and execute it only from your own wallet-controlled environment.
This skill never signs or sends. You must sign and broadcast from your own wallet.

### Start the existing agent and approve the policy

Keep the already configured WhatsApp sidecar and approved test-chat allowlist.
The read-only connection check succeeded, but no handset acceptance is claimed.
Stop the previous `agent whatsapp` process before starting one replacement; do
not start a second Arkiv writer or remove its lock. Preserve all database,
Redis and vault files. Start in the repository root:

```sh
AGENT_LENDING_MODE=testnet AGENT_LENDING_EVALUATOR=cre AGENT_LENDING_BINDINGS="$PWD/.local/lending-agent-setup/bindings.json" uv run agent whatsapp
```

Keep this process running. Ctrl-C stops it safely. Restart with the same command
and original data directory to resume tracking. No new wallet or provider login
is needed if the existing configured connection remains available. `cre whoami`
checks the existing CRE login; if it expires, the user completes `cre login`.

In the approved WhatsApp test chat:

```text
/lending plan
[read the actual review]
approve
/lending start
/lending status
[wait for funding receipts, scheduled evaluation and destination confirmation]
/lending pause
/lending withdraw
/lending status
```

Plain `approve` binds to the latest plan review in that authenticated chat. The
complete `approve lending PLAN_ID VERSION REVIEW_DIGEST` line remains supported.
Approval alone moves nothing. Start funds
both strategy wallets, sets their policy versions and deposits the source
principal. Management becomes active only after exact finalized receipts and
observed principal agree. The beginner schedule is hourly. For a faster live
acceptance run, request expert settings before approval:

```text
/lending plan {"amountUnits":"800000000000000000","reserveUnits":"200000000000000000","days":30,"checkIntervalSeconds":60,"maxObservationAgeSeconds":"1800","maxObservationSkewSeconds":"900"}
```

Receipt finality and CCIP delivery take time independently of the check interval.
Check actual status instead of assuming that a waiting period proves delivery.
Never approve a second plan to resolve a pending transfer.

### Restart, failure and withdrawal behavior

A per-user lock coordinates scheduler, manual requests, pause and withdrawal.
Every direct transaction is signed once, then saved with its hash and exact
intent before submission. A lost response remains pending until that same hash
has a canonical finalized receipt. Start intent is persisted before the first funding submission, so a restart
continues an already authorized start. The CRE operation is durably claimed before
invoking the broadcaster. A second caller never submits it again. An expired
instruction can be cancelled automatically only after finalized no-send proof
and a check that the owner has no pending transaction.

Destination success requires Part 3's receipt, token transfer and pool balance
checks. An API `SUCCESS` alone does not confirm a pool deposit. A caught pool
failure leaves tracked idle tokens; withdrawal returns those exact tokens to the
owner, verifies recovery and releases the corresponding source lock. Actual
CCIP execution failure remains tied to the original message. If protocol-level
manual recovery is required, the user must complete recovery of that message;
no replacement source transfer is invented. Unknown direct transactions or
reverted setup transactions can require operator reconciliation. Preserve the
journals and investigate their hashes rather than deleting state.

Withdrawal stops new investments, waits for pending operations, releases the
verified source lock and calls the new owner-only `withdrawToOwner` method.
Principal and BnM reserve return to the selected agent owner on their current
chains. LINK fee balances remain in the strategy wallets and are shown separately.
Source, funding and withdrawal gas quantities are retained separately from
fictional price estimates. Destination fee accounting follows the CCIP quote.

### External acceptance still required

The implementation is not a claim that the live MVP has passed acceptance.
The outstanding sequence is: faucet funding; user-run deployment; one configured
agent restart; the user's actual WhatsApp plan approval/start; finalized funding
and deposit; scheduled CRE decision and any permitted transfer; independently
verified destination deposit; actual WhatsApp status; pause and finalized
withdrawal. No private key should be pasted for any of these steps.

Public-webhook signature verification is not applicable to the current Redis
intake. This remains a trusted local-host integration. A publicly exposed or
multi-host deployment requires a separately verified authentication and
coordination design and is outside this testnet MVP.

## Short sample conversation

All entries below are from the **mock** path; IDs/digest are shortened here only.
Use the full exact values emitted by the app when running it.

```text
You: Manage my test tokens for 30 days. Keep some available and only move
     the rest when the expected extra interest covers the fees.
Ira: TEST PRESET. MOCK balances and execution. Available 1000 CCIP-BnM;
     deposit 800, keep 200 idle. Two approved testnet mock pools. Check
     hourly; cap 3 SIM_USD per move; minimum net improvement 1 SIM_USD.
     [Full deadline, assumptions and stop conditions, then exact approval reply.]
You: approve lending <full-plan-id> 1 <full-review-digest>
Ira: Exact plan approved. Use /lending start to verify funding and deposit.
You: /lending start
Ira: MOCK initial deposit submitted; not confirmed yet.
Ira: MOCK: Deposit confirmed on Ethereum Sepolia. No interest has accrued.
Ira: MOCK: No move [expected extra interest does not clear costs and minimum].
Fixture: Destination controlled rate changes from 7% to 20%.
Ira: MOCK: Transfer submitted.
Ira: MOCK: Still in transit. Destination deposit is not confirmed.
Ira: MOCK: Deposit confirmed on Base Sepolia. No interest has accrued.
You: /lending status
Ira: 800 deposited on Base Sepolia; 200 idle on Ethereum Sepolia;
     no tokens in transit. Fee token spent: 2 SIM_FEE_ASSET.
     Last projected extra interest about 10.52 SIM_USD, costs 2,
     projected net improvement about 8.52. Earned profit: 0.
You: /lending pause
Ira: Paused. Prepared operations cancelled. Submitted transfers remain monitored.
You: /lending withdraw
Ira: Withdrawal requested. Success requires a receipt.
Ira: MOCK: Withdrawal receipt confirmed on the listed current testnets.
```

## Recorded verification

See [the result index](evidence/part4/test-results.json) for exact commands and
scope. Contract tests exercise principal/reserve withdrawal, unauthorized and
excess withdrawals, active/failed inbound guards and the complete local
source-send → destination-deposit → principal-withdrawal sequence. Fault-injection
tests exercise the real disposable signer and durable transaction journal with
mock RPCs, including lost responses, restart, unfinalized/reverted receipts,
partial liquidity, gas caps, CRE admission and pause. These tests are not public
chain receipts.

The [mock conversation](evidence/part4/mock-conversation.txt) and
[native-CRE/mock-execution conversation](evidence/part4/native-cre-mock-conversation.txt)
remain labelled demonstrations. Neither transcript proves real handset delivery
or an onchain transaction. Live acceptance will require its own transaction and
message evidence after the funding/deployment steps above.
