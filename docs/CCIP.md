# CCIP-BnM transfers from WhatsApp

A user can ask Ira to send CCIP-BnM from Ethereum Sepolia to a recipient on Base
Sepolia. The existing wallet connection, chat approval, MetaMask page, encrypted
agent-wallet signer, task store and WhatsApp adapter handle the request. No new
payment key or wallet service is introduced.

The feature is implemented and locally tested. Deployment and a funded testnet
transfer from an actual WhatsApp handset remain setup and acceptance steps.

## Verified route and dependencies

Checked on **6 November 2026** against the official
[CCIP documentation](https://docs.chain.link/ccip),
[Ethereum Sepolia directory](https://docs.chain.link/ccip/directory/testnet/chain/ethereum-testnet-sepolia),
[Base Sepolia directory](https://docs.chain.link/ccip/directory/testnet/chain/ethereum-testnet-sepolia-base-1),
and [CCIP-BnM listing](https://docs.chain.link/ccip/directory/testnet/token/CCIP-BnM).
The read-only route checker also succeeded against both live public RPC endpoints:
Ethereum's Router reported support, the active OnRamp reported `OnRamp 2.0.0`,
and the registered token pool mapped this route to the expected Base token.

| Value | Ethereum Sepolia | Base Sepolia |
| --- | --- | --- |
| Network ID | `11155111` | `84532` |
| CCIP chain selector | `16015286601757825753` | `10344971235874465080` |
| Existing Router | `0x0BF3dE8c5D3e8A2B34D2BEeB17ABfCeBaf363A59` | `0xD3b06cEbF099CE7DA4AcCf578aaebFDBd6e88a93` |
| CCIP-BnM, 18 decimals | `0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05` | `0x88A2d74F47a237a62e7A51cdDa67270CE381555e` |

The observed source OnRamp was `0x8dcf17f298c881A547D91ca4aA3C2AD7568C6777`.
The source TokenAdminRegistry is `0x95F29FEE11c5C55d26cCcf1DB6772DE953B37B82`.
Its live registered pool was `0xabc0dF19Ce41b0b5e711366636e32F5914493B99`.
Pool addresses can change and directory snapshots can lag: the checker reads the
registry and validates the remote token instead of pinning a pool from a page.
The tracker discovers authorized destination OffRamps from the destination Router
and retains the ones observed for the intent across upgrades.

Pinned Solidity packages are recorded with SHA-512 archive checksums in
`contracts/dependencies.lock.json`:

- `@chainlink/contracts-ccip`: **2.0.0**, verified current registry release.
- `@chainlink/local`: **0.3.1**, verified current registry release; its local Router supports V3 arguments.
- `@chainlink/contracts`: **1.5.0**, required by Chainlink Local.
- OpenZeppelin Contracts **5.4.0** for our sender, plus **4.8.3** and **5.3.0** for upstream imports.
- Solidity **0.8.26**; Foundry Forge and Cast **1.7.1**, pinned in `package-lock.json`.

`npm run ccip:deps` downloads and verifies the Solidity source archives only. It
avoids installing the unrelated JavaScript build dependencies of the Solidity
packages. Foundry remappings resolve these sources under ignored `contracts/lib/`.
The bootstrap requires Node.js 22 or later and `tar`. The npm scripts invoke the
project's Foundry binaries and preserve their exit codes, even when a different
product called `forge` is installed globally.

## Values and tokens you supply

Copy `.env.example` to `.env` only if you do not already have `.env`. Preserve the
existing inference, WhatsApp, Arkiv and wallet configuration.

| Setting | What to supply |
| --- | --- |
| `AGENT_CCIP_SENDER_ADDRESS` | Address of your new CCIPSender deployment on **Ethereum Sepolia**, set after deployment. |
| `AGENT_SEPOLIA_RPC_URL` | Ethereum Sepolia JSON-RPC endpoint. The example public endpoint works; a dedicated endpoint is preferable for sustained use. |
| `AGENT_BASE_SEPOLIA_RPC_URL` | Base Sepolia endpoint supporting `eth_call`, `eth_getLogs`, receipts and the `finalized` block tag. |
| `AGENT_CCIP_MAX_FEE_ETH` | Maximum native CCIP fee you permit per transfer; default `0.01` ETH. Source gas is reviewed separately. |
| `AGENT_TASK_BACKEND` | Keep `arkiv`. Transfers are disabled in local-only SQLite mode. |
| Existing settings | Keep your inference configuration, Arkiv access key, dedicated WhatsApp line, allowed chats and sidecar credentials. |
| `AGENT_WALLET_PUBLIC_URL` | Existing HTTPS wallet endpoint, required for MetaMask connections and signatures. |

You also supply the recipient's **nonzero EVM address on Base Sepolia** and the
amount of CCIP-BnM. An EVM address is the normal `0x` wallet address; it does not
contain a network name, so the review explicitly names Base Sepolia.

Fund the **connected chat wallet** with:

1. **CCIP-BnM on Ethereum Sepolia**, at least the transfer amount. Use the official
   [CCIP test-token instructions](https://docs.chain.link/ccip/test-tokens) and
   [Chainlink faucet](https://faucets.chain.link/). Select Ethereum Sepolia and the
   exact CCIP-BnM address above. A first test with 1 CCIP-BnM is sufficient.
2. **Sepolia ETH** for the CCIP fee and source gas. The first transfer can have two
   source transactions: ERC-20 allowance approval and the CCIP send. Start with
   around 0.02 test ETH and check the live quote; that amount is a convenience,
   not a guaranteed fee requirement.
3. **Rabdi tGLM** for the project's existing Arkiv task checkpoints. MetaMask users
   still approve those checkpoints in MetaMask, on Rabdi.

The **deployment account** separately needs Sepolia ETH for deploying the sender.
It can be a different wallet from the chat wallet. No LINK is needed for this
native-fee flow. A recipient needs no Base Sepolia ETH merely to receive tokens;
it will need Base Sepolia ETH if it later sends them.

Do not put a user's private key in chat or `.env`. Local agent wallets reuse the
existing encrypted vault. MetaMask keys stay in MetaMask. The deployment commands
below use a local encrypted Foundry keystore.

## Exact installation and local tests

From the project root:

```sh
uv sync --frozen
npm ci
npm run ccip:deps
npm run ccip:test
npm run ccip:build
uv run pytest -q
npm test
```

The seven Solidity tests use **Chainlink Local Simulator**, not a replacement Router
written for this project. They exercise a nonzero native fee, token delivery,
surplus refund, exact allowances, duplicate-intent rejection, invalid inputs,
atomic failure, and V3 extra-argument encoding. Local Simulator routes synchronously
using its synthetic selector. It does not prove public testnet delivery, real
verifier operation, or WhatsApp handset behavior.

Python tests exercise the real authorization and signing code with deterministic
network responses: both wallet types, lost broadcast responses, cache recovery,
fresh approval between allowance/send, exact calldata matching, old-link rejection,
failed destination execution, finality checks, and task-completion evidence.

## Deploy to Ethereum Sepolia

Make sure `.env` contains both RPC endpoints. Foundry loads `.env` itself; never
replace an existing file just to deploy. Check the route before deployment:

```sh
npm run ccip:check
npm run ccip:cast -- wallet import ccip-deployer --interactive
npm run ccip:forge -- script contracts/script/DeployCCIPSender.s.sol:DeployCCIPSender \
  --rpc-url sepolia --account ccip-deployer
```

The last command is a simulation. Review its result, then broadcast:

```sh
npm run ccip:deploy
```

Cast prompts for your deployment key and an encryption password during import;
the key is not passed on the command line. Forge prompts for the keystore password
when running the deployment script. The script refuses other network IDs, verifies
that the official Router and token have code, and checks destination support.

Find the new CCIPSender address in Forge's returned `deployed` value or the
`contractAddress` of the creation transaction in
`broadcast/DeployCCIPSender.s.sol/11155111/run-latest.json`. Set:

```dotenv
AGENT_CCIP_SENDER_ADDRESS=0xYOUR_DEPLOYED_SENDER
```

Then validate the deployment and start the existing WhatsApp worker:

```sh
npm run ccip:check
uv run agent web3-init
uv run agent whatsapp
```

The route checker verifies both network IDs, Router support, OnRamp version, source
pool's destination token, both tokens' decimals and destination OffRamp presence.
When the sender address is configured, it also checks the sender's immutable Router,
token and selector. It only reads the chains. The Router, token pools and destination
contracts already exist: deploy only the sender on Ethereum Sepolia.

Use the project's existing sidecar pairing and HTTPS wallet service setup in
[WEB3.md](WEB3.md) and [the WhatsApp integration guide](../integrations/whatsapp/README.md).
Restart an already running worker to load the new tools and configuration.

## What the user sees

Connect through the existing wallet flow, or create an agent wallet. In WhatsApp:

```text
Send 1 CCIP-BnM from Ethereum Sepolia to 0xYOUR_RECIPIENT on Base Sepolia.
```

Ira creates a durable task, quotes the fee, and presents:

- CCIP-BnM amount, sender, recipient and destination.
- Sender contract / allowance spender.
- Current native CCIP fee and its maximum, including a 10% quote buffer.
- Maximum gas cost for the transaction being signed.
- Whether this step approves the token allowance or sends the transfer.
- An expiry and the existing `approve CODE` / `reject CODE` choices.

A first transfer usually needs the allowance step. It authorizes the sender to
pull exactly the requested token amount, rather than unlimited tokens. After that
transaction confirms, Ira saves its hash and sends a fresh review and approval
code for the CCIP send. Each step needs its own raw incoming approval. With
MetaMask, each approval returns a one-shot wallet link showing the same details.
Existing sufficient allowance skips the allowance step.

The send attaches the reviewed native fee cap. The contract calls Router `getFee`
again, refuses a fee above the cap, pays the actual fee in ETH and refunds surplus
in the same transaction. Source gas is additional. A rising fee can revert the
send; no retry broadcasts automatically. Refreshing an unsigned preview keeps the
same intent and requires a new approval. Once a signing request starts, refresh is
blocked and the existing transaction must be reconciled.

## Tracking, recovery and completion

Agent tools:

| Tool | Responsibility |
| --- | --- |
| `quote_ccip_fee(recipient, amount)` | Live fee, cap, balance and allowance reads; no signing. |
| `prepare_ccip_transfer(recipient, amount)` | One intent per task; returns the review or the existing saved intent on retry. |
| `refresh_ccip_review(transfer_id)` | Refresh only an unsigned preview, preserving the intent and requiring fresh authorization. |
| `ccip_transfer_status(transfer_id)` | Reconcile source submission and track destination execution; never resends. |
| `ccip_delivery_receipt(transfer_id)` | Evidence allowed only after finalized destination delivery for this task. |

The existing `transfers` records and encrypted Arkiv task checkpoints retain the
stable intent ID, allowance hash, exact transaction, source hash, CCIP message ID,
destination scan position, and destination hash. Signed source bytes and hash are
checkpointed **before** local broadcast. MetaMask request admission is one-shot and
persisted before the wallet receives its transaction. Hash recovery verifies the
exact sender, network, nonce, target, native value and calldata.

The contract also saves `messageIds(wallet, intentId)`. A second source send for
that wallet and intent reverts before pulling tokens. Repeated approval or task
preparation uses the original intent. Keep the same task when retrying or asking
for progress; creating an unrelated new task is a new payment intent.

The worker polls while running. Two source confirmations allow extraction of the
sender's matching `TransferSent` event and message ID. This is **in transit**, not
success. Delivery requires canonical finalized source inclusion, a finalized
successful `ExecutionStateChanged` event from an authorized Base OffRamp for the
same message and source selector, and a CCIP-BnM transfer to the intended recipient
in that destination receipt. A failed execution remains pending; later manual
execution can deliver the same message. Use the saved message in
[CCIP Explorer](https://ccip.chain.link/) for investigation, never a second source send.

`finish_task` rejects CCIP completion without `delivered` state and
`ccip_delivery_receipt` evidence. The delivery notice includes the source hash,
CCIP message ID and destination hash. Ordinary polling does not write a new Arkiv
checkpoint every few blocks. Full scan pages, execution observations and changed
OffRamp sets are persisted when needed; scans are bounded to 2,000 blocks per poll.

Use RPC endpoints that expose accurate receipts, logs and the `finalized` tag.
If a provider is unavailable or cannot verify delivery, the task stays pending.
The tracker deliberately waits for finality rather than promising an arrival time.
No CCIP API key is required because delivery is checked directly on the chains.

Preserve the existing vault, wallet files, Arkiv journals, wallet-link database and
WhatsApp deduplication database. Run one worker for this installation; independent
servers sharing signing authority need additional cross-process coordination.
Recovery depends on the existing Arkiv retention and key backup policy in WEB3.md.
