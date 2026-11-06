# A small agent that gets work done

This is an independent, extensible agent in the root of `notsureyet`. It keeps the
important ideas from Ira: own a task, use tools, save progress, remember the user,
wait when necessary, continue after a restart, and verify results before claiming success.

The runnable project is the small `agent/` package. `rc-harness copy/` is reference
material and is never imported. No original credentials or production data are used.

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
approve CODE_FROM_THE_REVIEW
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
support native ETH on Ethereum Sepolia and CCIP-BnM from Ethereum Sepolia to Base Sepolia.
CCIP uses native ETH fees and reports completion only after finalized destination delivery.
See [CCIP setup, deployment, funding and recovery](docs/CCIP.md) for exact commands.

The task list queries Arkiv directly. Startup reconstructs task progress, evidence
and payment hashes from encrypted immutable checkpoints before execution. SQLite
loss does not cause a submitted payment to be sent again. Checkpoints have 30-day
retention; the running worker refreshes unfinished tasks weekly. MetaMask wallets
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

See [the source map and design notes](docs/ARCHITECTURE.md) for what was learned from
rc-harness and how the smaller implementation carries those responsibilities.
