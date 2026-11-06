# A small agent that gets work done

## Multichain lending (Parts 1–2)

Part 2 adds hardened test contracts and a read-only CRE workflow.
`npm run lending:part2:test` runs authorization/encoding/adapter tests and compiles
the workflow to WASM. Arithmetic move/hold cases also pass in the actual CRE
simulator. The 11 deployment/funding transactions are confirmed. Finality and
deployed-contract move/hold acceptance remain pending, including the final
mock-rate change. See [the Part 2 guide](docs/lending-part2.md).

`npm run lending:examples` runs labelled, offline move/hold scenarios.
`npm run lending:test` verifies the shared TypeScript rules and application/CRE
interface. See [scope, arithmetic, architecture and verification](docs/multichain-token-manager.md).
This module makes recommendations only; lending execution and WhatsApp scheduling
are later parts.

## Arkiv integration: evidence and judging

Ira uses **Rabdi (chain 7794095)** through **@arkiv-network/sdk 1.0.1**. Task lists,
progress checks and restart recovery read Arkiv. The integration is a required
task-state dependency, rather than a write-only activity log.

Actual entity writers observed on 2026-11-06:

| Wallet | Records |
| --- | --- |
| `0x29a73a629FFd5323dDcA4A474210d38d2722D41d` | Agent storage wallet; v1 storage proof and v2 current-state/checkpoint demonstration |
| `0x66051Eef2e7DD1b410C6E68EeDF8ABBddB783Ee1` | Connected MetaMask wallet; v1 checkpoints for the original Sepolia payment request |

One independent query judges can run from this public repo, without our API or a
signing/decryption key:

```sh
npm ci
node integrations/read-tasks.mjs \
  0x29a73a629FFd5323dDcA4A474210d38d2722D41d \
  8ad85568f8a12be483ef170103a4bdf71d4a187113d74524b183d37bf7fb668b
```

Expected: one resolved v2 task, status `completed`, capability `simulation`, protocol
`chainlink_local`, and its saved simulation reference. The anonymous endpoint is
limited; configure your own `AGENT_ARKIV_ACCESS_KEY` in ignored `.env` and add
`--env-file=.env` to Node if needed. The command reads real Arkiv data and prints
public state. Private task contents require the separate encryption key.

Live v2 transactions:
[create current record + checkpoint](https://rabdi.explorer.arkiv.network/tx/0xa329e795b717259859ed93c6625e1e215ab37e4158d6b0709c910df0c49d0764),
[atomic update + new checkpoint](https://rabdi.explorer.arkiv.network/tx/0x1cbcd7ec615b2c6c77badb17063c3c931fa9f311d756bfd2e454c1cceb3fa3b4).
The same current-record key is retained. A new Ira runtime with an empty database
recovered this existing local simulation result from Arkiv using only authorized
encryption-key/scope access; no wallet signing files, payment journals, inference
calls, payment submissions or simulation reruns were used.
The real pending CCIP request was also recovered into a fresh Ira instance with
no signing keys or payment journals; its observed state was awaiting the user's
token-allowance signature. [Live pending-intent evidence](arkiv/evidence/live-pending-intent-recovery.json).

**Evidence boundary:** the latest saved cross-chain result was a local simulation
of 0.0001 CCIP-BnM; its displayed 0.001 ETH was the simulated fee. This is not proof
of a public Sepolia-to-Base ETH transfer. A separate controlled-RPC regression
recovers an already-submitted CCIP intent, verifies destination delivery and finishes
through the existing evidence tools with zero payments after restart.
[Demo, reports and reproduction](arkiv/demo.md).

Required judging files for Build on Arkiv:
[schema](arkiv/schema.md), [feedback](arkiv/friction.md), [reach](arkiv/reach.md),
[why Arkiv](arkiv/why-arkiv.md), [how it is used](arkiv/how-you-use-arkiv.md).
Criteria source: [ETHGlobal Mumbai Arkiv prizes](https://ethglobal.com/events/mumbai/prizes/arkiv).
Track enrollment/eligibility is not verified by the implementation.

### Existing foundation, new work, license and AI use

The existing Ira-style foundation includes chat, tools, background tasks,
completion evidence, waits, wallets and payment approval. This contribution adds
queryable current task state, atomically paired encrypted checkpoints, one lifecycle
across those capabilities, protocol references, terminal-safe resolution and clean
recovery. We do not claim the underlying Ira product was created at this event;
the Continuity track requires the team's actual registration and disclosure.

Repository: [anushrut-rumik/notsureyet](https://github.com/anushrut-rumik/notsureyet).
Project code is under the [MIT License](LICENSE); dependencies retain their licenses.
OpenAI Codex assisted with implementation, tests, debugging and documentation.
The Arkiv skill/plugin and official SDK/docs informed schema and write-safety work.
The running agent also uses configured OpenAI models for inference. AI-generated
claims were checked against source, regression tests and the explicitly identified
live receipts/readbacks; simulations are labelled separately.

This is an independent, extensible agent in the root of `notsureyet`. It keeps the
important ideas from Ira: own a task, use tools, save progress, remember the user,
wait when necessary, continue after a restart, and verify results before claiming success.

The runnable project is the small `agent/` package. No original credentials or
production data are used.

## Run

1. `inference.toml` uses OpenAI `gpt-6-luna` for chat, planning, and workers.
2. Set `OPENAI_API_KEY` in this project's ignored `.env`.
3. Start:

```sh
uv sync --frozen
npm ci
uv run agent web3-init
uv run agent doctor
uv run agent chat
```

Create or connect your wallet in chat, then fund that same address with Rabdi test
tGLM for Arkiv storage. Configure `AGENT_ARKIV_ACCESS_KEY` locally. Arkiv is the default task authority.
`web3-init` initializes local encryption; it does not create a separate task wallet.
The terminal
starts the agent and its background task worker together. `/tasks` queries Arkiv;
`/quit` closes it. Unfinished tasks resume from unexpired Arkiv checkpoints when you start it again. The application
must be running for timers to fire; overdue waits resume on the next start.

For a fresh checkout, copy `.env.example` to `.env` before adding keys. Inference uses the key configured locally on this machine. Keep keys in `.env` or environment
variables; do not commit them.

`agent doctor` checks the live model catalog without paid inference. To test real
credentials and a tool-call round trip, run `uv run agent doctor --live`; this makes
six paid requests. See [OpenAI inference setup](docs/OPENAI.md).

## What it can do now

- Chat with persistent conversation history and saved user preferences.
- Create tasks with a completion condition and work through multiple tool steps.
- Read web pages; search the web with your optional Exa key.
- Calculate, create files, read them back, and verify the result.
- Delegate a focused subproblem to a separately configurable worker model.
- Save checkpoints, wait for a time, send reminders, or ask for missing information.
- Keep a task journal and action receipts. Unknown actions are inspected before retrying.
- Resume interrupted tasks and keep different users' records separate.

Generated files live in `workspace/`. Task checkpoints live on Arkiv;
`.local/agent.sqlite3` is their execution cache and stores local chat/preferences.
Private encryption/signing recovery files stay in `.local/`. `agent config` shows routes and whether keys are present,
without showing the keys themselves.

## Wallet work in WhatsApp

The product works through chat. MetaMask setup and signing use small linked wallet pages. Wallet creation,
balance reads, payment review/approval and task status use the same chat handler.
The existing Ira whatsmeow sidecar adapter is supported through its Redis event
stream and authenticated send API; configure the `AGENT_WHATSAPP_*` settings for
a dedicated test line and run `uv run agent whatsapp`.

```text
/wallet create
/balance
/transfer 0.001 0xRECIPIENT_ADDRESS
approve
/tasks
/receipt TRANSFER_ID
```

Choose a separate encrypted agent wallet with `/wallet create`, or connect your existing
MetaMask Mobile wallet with `/wallet connect`. MetaMask payments return a wallet
confirmation link after chat approval. For the separate-wallet mode, fund it with **Sepolia ETH**;
that same address needs **Rabdi tGLM** on Rabdi for application checkpoints.
Connecting an address by text only enables watching; MetaMask connection uses a signed
message, and each MetaMask payment still requires wallet confirmation. The model prepares a
payment; only an explicit incoming user approval can admit signing. Transfers
support native ETH on Ethereum Sepolia, native ETH on Base Sepolia through MetaMask,
and CCIP-BnM from Ethereum Sepolia to Base Sepolia.
CCIP uses native ETH fees and reports completion only after finalized destination delivery.
See [CCIP setup, deployment, funding and recovery](docs/CCIP.md) for exact commands.

To fund the agent from an existing MetaMask balance, send a WhatsApp message such as
"Send 0.005 ETH on Base Sepolia from my connected MetaMask to my agent wallet."
Approve the exact review in chat, then open the signing link and confirm in MetaMask.
Replying `approve` selects the latest payment review shown in that chat; `reject`
cancels it. Explicit codes remain supported when selecting a particular review.
An expired review is refreshed on the same payment and shown again before approval.
The transfer stays on Base Sepolia. Ethereum Sepolia needs its own ETH funding.
Base's execution gas estimate excludes its additional L1 data fee; MetaMask shows
the full network fee before confirmation. The default per-payment limit is 0.01 ETH.

Ask "Check ETH, CCIP-BnM and LINK in my MetaMask and agent wallets on both testnets"
to read their balances. The `wallet_balances` chat tool reads Ethereum Sepolia and
Base Sepolia directly, including token decimals and the observed block. It works
before lending or CCIP deployment and needs no transaction approval. Unavailable
reads are reported as unknown rather than zero.

Same-chain CCIP-BnM transfers also work through MetaMask. For example, ask
"Send 0.2 CCIP-BnM from my MetaMask on Base Sepolia to my agent wallet on Base Sepolia."
Reply `approve`, then confirm the exact token transfer in MetaMask. This calls the
token's `transfer` method directly, with ETH for gas and no bridge or allowance step.
The default per-transfer CCIP-BnM limit is 1 token. Confirmation verifies the exact
token Transfer event, including supported MetaMask smart-account execution on Base.

Base Sepolia receipt checks also support MetaMask's sponsored, single native transfer
through its DelegationManager and known smart-account implementation. The agent
checks the exact sender, recipient, amount, successful execution event and canonical
receipt. It recovers missing browser callbacks from indexed on-chain events without
asking MetaMask to send again. Batches and other delegated execution modes are not supported.

The task list queries Arkiv directly. Startup reconstructs task progress, evidence
and payment hashes from encrypted immutable checkpoints before execution. SQLite
loss does not cause a submitted payment to be sent again. Active records have 30-day
retention and terminal records have 180-day retention; the worker refreshes unfinished tasks weekly. MetaMask wallets
require approval for each refresh.

Arkiv selects the storage wallet dynamically for each chat. `/arkiv wallet agent`
uses the agent-created wallet for new tasks while MetaMask remains connected for payments.
`/arkiv wallet connected` selects MetaMask; `/arkiv wallet active` restores the default selection. Agent-created wallets use
their existing encrypted signing key. Connected MetaMask wallets approve each
checkpoint through a Rabdi signing link; work waits for confirmed storage.
`/arkiv` shows your active address, live tGLM balance and public query scope.
Fund that address on Rabdi; Sepolia ETH at the same address is a separate balance.
New tasks never use the old application-wide writer. Existing records remain readable,
and unfinished tasks retain their original wallet identity when a wallet is switched.

Independent query, using the address and scope printed by `/arkiv`:

```sh
node --env-file=.env integrations/read-tasks.mjs WRITER_ADDRESS INSTANCE_ID
```

Public reads of the supplied MetaMask address succeeded on Rabdi and Sepolia.
Automated tests cover per-user wallet selection, isolated recovery, pending MetaMask
checkpoints across restart, and stopping external effects until storage is approved.
They also exercise the full model-preparation → raw-chat approval → signing
→ receipt → task-completion flow with simulated networks, plus cache-loss recovery
and real SDK query/unsigned-estimation paths.

On 2026-11-06, the funded agent wallet successfully created and updated the encrypted
“Arkiv live storage proof” task on Rabdi. Both receipts and entity creator/owner were
independently checked, and an empty SQLite cache recovered the completed task.
[Create transaction](https://rabdi.explorer.arkiv.network/tx/0x8c341a16ec19209ee1566b56ef883ee2dc6549e2875db8e7f09defe7d98787ca),
[update transaction](https://rabdi.explorer.arkiv.network/tx/0x4e72ea2a1dd8941c1fe95aeeea4af6683d39ed31ba76abe905f629e4535fa3f4).
The two writes cost 0.000512504 tGLM total. This validates storage and cache recovery;
it does not establish that a payment was sent or a handset received a message.
The Arkiv Codex plugin 0.5.4 was installed; its Mumbai MCP endpoint responded.

See [setup, chat commands, custody and verification limits](docs/WEB3.md),
[Arkiv schema](arkiv/schema.md), [friction](arkiv/friction.md),
[reach](arkiv/reach.md), [why Arkiv](arkiv/why-arkiv.md), and
[how it is used](arkiv/how-you-use-arkiv.md).

## Separate models and keys for inference

All roles use OpenAI `gpt-6-luna` with the same `OPENAI_API_KEY`. The Responses API
preserves reasoning state through tool calls. The routes are:

```toml
[routes]
default = "general"
chat = "general"
planner = "planning"
worker = "workers"
```

Edit the existing `general`, `planning`, and `workers` endpoints to change models
or use separate keys. Set each endpoint's `api_key_env` to the appropriate variable
in `.env`. Each endpoint has its own URL, model, key variable, and protocol.
Supported protocols: `chat_completions`, `responses`, and `anthropic`. Provider-specific
request settings go in the endpoint's `options` table. An unauthenticated local model
can omit `api_key_env`. There is no fallback to a different provider's key.
The configured provider is `openai`. The optional `generic` provider label remains
available for other compatible endpoints. `agent doctor` checks model availability;
`agent doctor --live` also verifies tool calling with real requests.

## Add a tool

Tools are ordinary async Python functions. See `examples/my_tools.py` for a complete
example. Enable it with `AGENT_TOOL_MODULES=examples.my_tools`, then restart.

Use `effect=True` for tools that change something outside the agent. Their action
is recorded before execution and a successful identical call is not blindly repeated
within the same task. `evidence=True` makes successful results eligible as completion
evidence. The agent still has to judge whether the evidence proves the requested outcome.

## HTTP API and tests

```sh
uv run agent serve
uv run pytest
```

The API listens on `127.0.0.1:8000`. Interactive API documentation is at `/docs`.
Endpoints include `/chat`, `/tasks`, `/events`, and `/tasks/{id}/reply`.
Run one agent process per project database. This is a local application, without
production authentication or multiple-worker coordination.

This version preserves Ira's task-ownership approach; it is not a full copy of every
production integration. Browser clicks, Gmail, phone calls and other external actions can be added as tools
with your own accounts. The WhatsApp sidecar adapter is implemented but needs
configuration for a dedicated line; no live WhatsApp delivery is claimed.
Provider tests cover OpenAI reasoning-state replay, tool results, usage reporting,
and error handling. Live checks require configured credentials and model access;
see [OpenAI setup and validation](docs/OPENAI.md). Different models can make
different decisions, so integration checks do not establish reliability for every task.

See [the architecture and design notes](docs/ARCHITECTURE.md) for the components,
data flow and responsibilities of this implementation.

Part 3 lending execution, monitoring and recovery are documented in
[docs/lending-part3.md](docs/lending-part3.md). Run `npm run lending:part3:test`
for the focused local checks. Live testnet deposit acceptance remains pending;
Part 2 wallets and setup are preserved.

### WhatsApp lending — Part 4

Run `npm run lending:part4:demo -- --scripted --evaluator test-harness` for the
complete **mock** conversation/lifecycle, or use `--evaluator cre` for native local
CRE arithmetic evaluation. `npm run lending:part4:test` runs focused verification.
The [Part 4 runbook](docs/lending-part4.md) explains setup, authenticated approval,
scheduling, recovery and the live agent-wallet broker. Automatic policy execution,
funding/deposit and principal withdrawal are implemented and locally tested. The
selected wallet still needs faucet funding and new compatible deployments; the
full live WhatsApp/CCIP/withdrawal acceptance remains pending.
