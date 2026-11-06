# Part 2 setup and evidence

Part 2 supplies hardened mock contracts, user-run testnet setup artifacts, a
read-only CRE workflow and tests. **The contracts were not yet deployed or
funded at the initial implementation stage. Subsequently, all 11 user-signed
deployment/funding actions were confirmed through the wallet review. Base
Sepolia finality, the final mock-rate change, and actual deployed-data move/hold
acceptance remain pending. CRE login is verified, and both arithmetic cases
have run successfully in the actual simulator. Deployed-contract acceptance
still needs finality and the move/hold runs to pass.** The arithmetic evidence is
separate from proof of reading testnet positions.

## What the contracts permit

Each test user gets a new pool and strategy wallet on each chain. Pools account
for principal by caller. A wallet's owner, pool, asset, router, LINK, forwarder,
workflow ID, reserve and amount limit are immutable. Peer and policy version can
only be changed by that owner. The same public test signer owns that test user's
pair; do not use an existing messaging or production wallet. Repeat deployment
for each further test user. There is no shared unaccounted wallet balance.

`MockPool` transfers only the caller's own deposited principal on withdrawal.
It rejects zero/over-capacity deposits and tokens whose received amount differs
from the supplied amount. Its owner can change the displayed APR. The APR is
RAY-scaled: `4e25` means 4%, and dividing by `1e23` yields 400 basis points.
Fractional basis points are rejected instead of flooring each rate separately.
**The mock never accrues interest.**

`TestStrategyWallet` protects idle BnM on deposits and idle withdrawals. Pool
withdrawals return funds to that same wallet. Only its dedicated owner can move
funds, change peer/policy or recover LINK. The tutorial's public
`withdrawFromPoolAndDepositCrossChain` is replaced with an unconditional revert.
No CCIP receiving callback exists: every inbound source/sender pair is rejected.
No approved pair can execute a transfer yet. Future CCIP execution, all-token
accounting, pair allowlists, tracking and recovery remain Part 3.

The report consumer is an **admission test**, not a rebalance executor. It checks:

1. Exact forwarder, workflow ID, ten-byte workflow name and owner metadata.
2. An independent owner EIP-712 signature, using domain
   `LendingTestInstruction`, version `1`, actual chain ID and consumer address.
3. Operation ID, source chain ID, consumer, destination selector and peer, source
   token, amount, policy version and expiry. Amount must fit the policy limit and
   existing deposited principal; idle reserve must still be present.
4. Canonical raw ABI encoding and a previously unused operation ID. Consumption
   happens atomically only after all checks pass. A changed policy invalidates
   old signed instructions. At the expiry timestamp the instruction is rejected.

The report is ABI `(Instruction, bytes signature)`. Function calldata adds a
four-byte method selector and is **not the report payload**. Viem produces a
committed golden encoding vector; Solidity decodes it in a separate regression
test. Other tests cover forged public-forwarder metadata without a valid signer,
field tampering, replay, expiry, policy revocation, reserve protection, mainnet
rejection, identity errors and user isolation. Report admission consumes no funds
and creates no CCIP transaction. Metadata by itself proves no user approval.

## Infrastructure checked on 2026-11-06

Only Ethereum Sepolia (`11155111`) and Base Sepolia (`84532`) are configured.
Their CCIP selectors are decimal strings `16015286601757825753` and
`10344971235874465080`. Selectors identify CCIP chains; they are not EVM chain IDs.
The complete addresses and read-only RPC results are in
[evidence](evidence/part2/testnet-preflight.json) and `lending/testnets.ts`.

The live preflight checked each RPC's actual chain ID, code at the router,
BnM, LINK and simulation forwarder, 18-decimal tokens, direct lane support,
OnRamp 2.0.0, and the registered pool's remote token mapping in **both directions**.
These are infrastructure addresses, not our owned deployments. The live Sepolia
registry returned BnM pool `0xabc0dF19Ce41b0b5e711366636e32F5914493B99`, while the
directory token table still displayed a different older pool. The checker uses
the live registry and remote-token mapping; it does not copy the older pool into
our deployment manifest.

Official sources read:

- [Ethereum Sepolia directory](https://docs.chain.link/ccip/directory/testnet/chain/ethereum-testnet-sepolia)
- [Base Sepolia directory](https://docs.chain.link/ccip/directory/testnet/chain/ethereum-testnet-sepolia-base-1)
- [BnM directory](https://docs.chain.link/ccip/directory/testnet/token/CCIP-BnM)
- [CRE simulation guide](https://docs.chain.link/cre/guides/operations/simulating-workflows)
- [Workflow CLI reference](https://docs.chain.link/cre/reference/cli/workflow)
- [TypeScript SDK](https://docs.chain.link/cre/reference/sdk/overview-ts)
- [TypeScript runtime](https://docs.chain.link/cre/concepts/typescript-wasm-runtime)
- [Forwarders](https://docs.chain.link/cre/guides/workflow/using-evm-client/forwarder-directory-ts)
- [Consumer contracts](https://docs.chain.link/cre/guides/workflow/using-evm-client/onchain-write/building-consumer-contracts)

Simulation forwarders currently resolve to
`0x15fC6ae953E024d975e77382eEeC56A9101f9F88` on Sepolia and
`0x82300bd7c3958625581cc2F77bC6464dcEcDF3e5` on Base Sepolia. The directory says
`cre workflow supported-chains` is authoritative for the logged-in organization;
verify it after login before any future report-delivery testing. Part 2 does not
call those forwarders. Production forwarders are different. Consumer metadata
accepts both the 62-byte logical layout and the 64-byte production layout.

## Actual CRE arithmetic verification after login

A separate `arithmetic-check` workflow was scaffolded with the authenticated CLI,
then its generic sample handler, tests, config and unused dependencies were
replaced before simulation. It calls the same shared evaluator and accepts only
observations labelled `arithmetic-fixture:`. It has no EVM or HTTP client and
returns `deployedContractsRead: false`. The installed CLI requires RPC settings
at startup even for this arithmetic handler, so the `local-simulation` target
lists only the two verified testnet RPCs. It does not read our missing contracts.

Both actual CLI runs exited successfully and matched local evaluation at the
same returned timestamp:

| Arithmetic case | Decision | Net improvement, micro-SIM_USD | Exact local match |
| --- | --- | ---: | --- |
| 60-day fixture | move | 1,945,194 | yes |
| 30-day fixture | hold | −27,408 | yes |

The time remaining decreased during compilation, which explains the small
change from Part 1's frozen-clock amounts. No policy threshold changed. These
are fictional arithmetic amounts, not deposited balances or earned interest.

Evidence includes the immutable input, exact command, full output and local/CRE
results: [move](evidence/part2/native-arithmetic-verified/move-evidence.json),
[hold](evidence/part2/native-arithmetic-verified/hold-evidence.json).
Reproduce from the repository root into a new directory:

```sh
npm run lending:cre:arithmetic -- .local/new-native-arithmetic-evidence
```

The wrapper invokes `cre workflow simulate arithmetic-check --target
local-simulation --non-interactive --trigger-index 0 --http-payload` with each
actual input path, from `cre-lending`. Neither run broadcasts. The deployed-data
workflow remains unchanged in purpose and still needs a funded owned manifest.

## Create the dedicated test wallet

Run these commands yourself in a terminal. The directory and account-name
arguments make Foundry save an **encrypted** keystore; do not omit them. The
installed Foundry 1.7.1 help confirms that the password prompt is hidden by default.

```sh
cd /Users/bhaskarpandit/Documents/notsureyet
mkdir -p "$HOME/.foundry/keystores"
node scripts/foundry.mjs cast wallet new "$HOME/.foundry/keystores" lending-test-only
```

Choose the encryption password locally. Keep the keystore and password on your
computer; share only the printed public `0x...` address. The agent does not invoke
this command, inspect the file or request the password. If this account already
exists, use that dedicated test account or choose a new unused test name; do not
force an overwrite. A later Forge invocation can select `--account
lending-test-only` and prompt for its password without exposing signing material
in command arguments.

The same address needs test ETH for gas on both Ethereum Sepolia and Base
Sepolia, plus test LINK and BnM. Use [Chainlink Faucets](https://faucets.chain.link/)
for ETH/LINK. If a faucet requires a browser wallet, obtain faucet tokens there
and transfer only those test tokens to the new public address using your own
wallet. BnM on EVM chains comes from the token's `drip(address)` method; the
[official guide](https://docs.chain.link/ccip/getting-started/test-tokens) explains
it. Once the public address is known, the agent can check balances and prepare
exact BnM and setup transactions for user-run signing. No manifest is required
before wallet creation: it is the public record of the later contract deployments.

## Prepare a concrete public test plan

Do not read or export a private key to the agent. Create/import a dedicated
faucet-only signer through your own wallet interface. Its **public address** is
sufficient for every read-only check here. Keep signer custody in your wallet or
encrypted Foundry account; the agent does not inspect that account.

Copy `examples/lending-testnet-plan.json` to an ignored local path. Nulls are
intentionally rejected. Fill in the public signer, workflow ID, absolute approval
and deadline timestamps, actual principal/reserve/limit/capacity, explicit funding
amounts and estimated costs. Workflow ID is a nonzero bytes32 identity for this
test consumer; it must match metadata if report admission is tested later. Policy
version is a string and its onchain representation is `keccak256(UTF-8 version)`.
Amounts are integer token units, not human-readable decimal tokens. Keep the
minimum benefit and maximum fee limits that you reviewed; do not reduce them to
manufacture a move.

The example includes explicit fictional `SIM_USD` prices. BnM has no reliable
market dollar value. These prices do not claim ETH, LINK or BnM market prices.
Source and destination approval gas estimates must be nonzero and reviewed.
The source's pool approval is inside the future source transaction, so its cost
belongs to `sourceGas`; `sourceApprovalGas` is explicitly zero. Destination
execution is counted inside `bridgeFee`; only an additional separately paid
approval belongs to `destinationApprovalGas`. Initial setup gas is sunk and does
not become another moving cost. The template seed initially records zero sunk
costs; replace those accounting fields with measured setup costs if presenting
strategy totals. No actual fee quote or funding is inferred from the seed.

```sh
npm ci
npm run lending:part2:test
npm run lending:testnet -- preflight
npm run lending:plan -- .local/my-completed-plan.json .local/lending-review
npm run lending:testnet -- preflight "$TEST_OWNER"
```

`TEST_OWNER` is only the public address. The last command returns gas wei, BnM
units and LINK units per chain without reading a signing secret. Use
[Chainlink Faucets](https://faucets.chain.link/) and your chosen Base Sepolia gas
faucet from your wallet/browser to obtain test ETH, test LINK and BnM. Exact gas
needs depend on estimates at execution time. Do not assume a balance is adequate
merely because it is nonzero. Fund the **actual** principal plus protected idle
reserve; Part 1's 800/200 fixtures impose no testnet balance requirement.

## User-run deployment and funding

The inspected upstream `DeployTokenManagerContracts` script deploys **both**
chains and revisits Sepolia in one run. It was not invoked. Our replacement
`DeployLending` script deliberately handles **one chain per invocation**, checks
the actual chain ID, and deploys no tutorial addresses. It uses constructor
arguments from `deployment-review.json` generated above.

Prepared on-chain action for user-run execution:

- Action: Deploy a new MockPool and dedicated TestStrategyWallet, once per testnet.
- Network: Testnet only; actual IDs 11155111 and 84532 are enforced.
- Source chain: The selected deployment entry in `deployment-review.json`.
- Destination chain: The opposite Sepolia testnet; no cross-chain send occurs.
- Route/lane: The verified direct lane and fixed selectors in that entry.
- Token/amount: Exact `principalUnits`, `idleUnits` and `linkFundingUnits` from your reviewed plan; deployment itself moves no tokens.
- Payload: The generated tuple argument, including owner, workflow ID, policy version hash, reserve, maximum amount, controlled rate and capacity.
- Contracts: Official infrastructure from `lending/testnets.ts`; new owned contracts returned by Forge.
- Method: `DeployLending.run((address,bytes32,bytes32,uint256,uint256,uint256,uint256))`.
- Expected effect: New per-user pool and wallet on the selected chain; no initial deposit or rebalance yet.
- User-run artifact: `deployment-review.json` and `contracts/script/lending/DeployLending.s.sol`.

Review this carefully and execute it only from your own wallet-controlled environment.

For each entry, invoke the pinned Forge wrapper with the entry's exact tuple and
RPC. `DEPLOYMENT_TUPLE` is the generated public tuple; `TEST_OWNER` is the public
address. Use the dedicated encrypted account that you control. Run without the
Forge broadcast option first and inspect the gas estimate; then the following
user-run command performs the **setup deployment transaction**:

```sh
node scripts/foundry.mjs forge script contracts/script/lending/DeployLending.s.sol:DeployLending --sig 'run((address,bytes32,bytes32,uint256,uint256,uint256,uint256))' "$DEPLOYMENT_TUPLE" --rpc-url "$TESTNET_RPC" --account lending-test-only --sender "$TEST_OWNER" --broadcast
```

Set `TESTNET_RPC` to the entry's verified public RPC. Wallet credentials are never
command arguments. Record each real creation transaction and contract address in
`manifest.json`. A zero hash is not a receipt. Wait for successful finalized
receipts. The read adapter verifies creation addresses and transaction senders.

Next generate unsigned funding actions:

```sh
npm run lending:funding:artifacts -- .local/lending-review/manifest.json .local/lending-review/deployment-review.json .local/lending-review/funding-actions.json
```

Prepared on-chain action for user-run execution:

- Action: Set each wallet's peer, fund it with reviewed test BnM and LINK, and make the initial source deposit.
- Network: Ethereum Sepolia and Base Sepolia only.
- Source chain: The action's explicit chain ID.
- Destination chain: Opposite-chain peer from your manifest; this setup sends no CCIP message.
- Route/lane: The previously verified direct lane; peer is this same test user's wallet.
- Token/amount: Exact token addresses and integer amounts in `funding-actions.json`; principal is zero on the destination.
- Payload: Each unsigned action's full calldata, target, sender, value and decoded arguments.
- Contracts: Your manifest's newly deployed wallet/pool and official BnM/LINK contracts.
- Method: `setPeer`, token `transfer`, then source `depositToPool`.
- Expected effect: Separately funded LINK, a real source lending position and a protected idle BnM reserve on both wallets; destination lending principal remains empty.
- User-run artifact: `funding-actions.json`; equivalent script `ConfigureAndFundLending.run(wallet,peer,principal,idle,linkAmount)` is also provided.

Review this carefully and execute it only from your own wallet-controlled environment.

Execute the ordered unsigned actions in your own wallet. Check the wallet's
starting principal/token balances are zero before first funding; do not repeat
funding on uncertainty. Record every successful finalized setup transaction in
that chain's `fundingTxs` manifest array. The optional combined funding script
rejects already-funded wallets and verifies the deposit/reserve postconditions.
The adapter independently rereads the resulting balances.

## Create immutable observations and run CRE

```sh
npm run lending:testnet -- prepare .local/lending-review/manifest.json .local/lending-review/seed-run.json .local/lending-review/move-input.json
cd cre-lending
cre workflow simulate workflow --target staging-settings --non-interactive --trigger-index 0 --http-payload ../.local/lending-review/move-input.json
```

Return to the repository root and use the wrapper to save complete evidence:

```sh
npm run lending:testnet -- simulate .local/lending-review/move-input.json .local/lending-review/move-evidence.json
```

Preparation verifies actual chain IDs, successful finalized owner receipts, pool
and wallet code, wallet configuration binding, actual principal, reserve, rates,
capacity, funding, decimals, and a router LINK quote. It **never** changes policy
limits or upgrades an empty position into a deposit. A requested amount greater
than the actual deposit is rejected. The seed's unobserved values are replaced
only by onchain observations; its policy, requested amount, valuation assumptions
and reviewed gas/exit estimates are retained. All objects passed to the evaluator
are copied and frozen.

Each run contains its own user/approval context, operation ID, block hashes,
block numbers, timestamps and observed values. Static workflow configuration has
no user policy or address. CRE checks finality and re-reads the exact pinned
blocks, including the onchain actual chain ID. It recomputes the wallet's complete
configuration binding from the policy and both per-user deployments. A changed
observation fails rather than silently rewriting the input. CRE returns run ID,
policy version, observation context and its evaluation timestamp. The wrapper
uses that same timestamp for local comparison.

The historical block must be within the user's freshness limits. Ethereum's
finalized block can be older than a five-minute limit, so the template test plan
explicitly displays 1800-second age and 900-second skew limits for review.
Preparation does not silently widen an existing policy. If a completed plan uses
stricter limits and observations are too old, the correct outcome is blocked.

`quoteLinkUnits` is an actual router `getFee` result in original 18-decimal LINK
units for a candidate token-plus-data message with 300,000 destination gas and
full finality. It is not a delivery receipt or an execution guarantee. The
candidate data is `abi.encode(amount)`; Part 3's authorized transfer payload and
receiver may need a different gas limit and fee quote. Both chains' candidate
quotes are recorded separately. Only the source quote enters this direction's
`bridgeFee`. Fictional prices and estimated gas/exit costs remain visibly
simulated; actual chain observations retain their block provenance.

For a controlled hold scenario, use the destination pool owner's unsigned
`controlledHoldScenario` action to set its displayed rate to 4%, equal to the
source's default rate. This is a separate user-run setup transaction, **outside**
the dry run. Create a fresh run ID, operation ID, snapshot ID and observation
reference with the same policy/limits, then prepare a new immutable input after
the rate transaction finalizes and simulate it with a new evidence filename.
No previously stored input or workflow config is rewritten.

With small faucet balances, 4% versus 7% may correctly hold under the reviewed
minimum benefit and actual fees. To exercise a move, explicitly review a higher
**controlled mock rate** in the completed plan, or a sufficiently funded amount
within the original approved limit. The model permits at most 1,000,000 basis
points. Never claim that extreme demonstration APR is a real yield, and never
lower fee or benefit limits to force the action. If the move condition still
fails, retain the hold/blocked result and explain its calculation.

Both completed dry-run evidence files must show `balancesUnchanged: true` and
`localMatchesCRE: true`. Their actual actions must be `move` and `hold`
respectively before claiming the live acceptance criterion has passed.

## Actual verification and remaining work

The Part 2 checks pass: 15 Solidity tests, 8 adapter/artifact/encoding/evidence
checks and 10 CRE SDK harness tests, plus successful WASM compilation. Existing
checks passed: 19 JavaScript tests, 35 Part 1 tests, 7 CCIP contract tests and 185
Python tests (2 paid tests skipped). `git diff --check` passed.

Evidence logs are split into ASCII-escaped chunks below 1 KiB and reassembled
with completeness checks. This respects the current
[CRE log-line quota](https://docs.chain.link/cre/service-quotas) without losing
Unicode identity fields or accepting truncated output. The handler launches 14
independent reads, below the documented concurrency limit of 30.

See [initial build and tests](evidence/part2/build-and-tests.txt),
[10 SDK tests after login](evidence/part2/cre-sdk-tests-after-login.txt),
[deployed-data workflow rebuild](evidence/part2/cre-build-after-login.txt), and
[initial simulator attempt](evidence/part2/simulator-attempt.txt). CLI v1.39.0 flags were
checked before use. The first attempts reported missing login; after the user
completed browser login, `cre whoami` confirmed authentication. Deploy access is
not enabled, but the arithmetic simulations succeeded without it. No credential
values or account identifiers are stored in the evidence.

Versions: TypeScript 5.9.3, application Zod 4.6.7, viem 2.57.16, CRE SDK 1.23.0,
Javy plugin 1.7.0, Javy 8.1.0, Bun 1.3.2, Solidity 0.8.26 and existing Foundry
1.7.1/CCIP 2.0.0 dependency lock. Existing application, payment and messaging
paths are preserved. The SDK requires Zod 3.25.76 internally for its function
schema API. `npm audit` reports two high findings through that dependency
([advisory](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5)). A blanket Zod 4
override would break that API. Run payloads are capped at 64 KiB before parsing;
configuration is a small fixed object, and no public service or DON workflow is
deployed. Reassess the SDK dependency before broader exposure.

Remaining user actions are: identify a dedicated
public test signer and reviewed completed plan; obtain faucet funds; execute the
reviewed testnet deployment/configuration/initial-deposit transactions; fill the
manifest with successful receipts; then prepare and run move/hold evidence. No
secrets were read, no wallet was reused and no chain transaction was sent here.
There is no actual testnet fee quote for our missing deployments yet and no
deployed-contract dry-run balance proof. No real WhatsApp users are connected, no
interest earnings are claimed and local testing is not decentralized execution.

This skill never signs or sends. You must sign and broadcast from your own wallet.
