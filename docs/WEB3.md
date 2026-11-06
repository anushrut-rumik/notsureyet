# Web3 work happens in chat

Ira creates a separate agent wallet. The user funds it with Sepolia test ETH and
approves each payment in WhatsApp. Connecting an address by text is watch-only. MetaMask Mobile can instead connect
through a short-lived link from WhatsApp and sign each payment in MetaMask.
There is no product dashboard; the two small wallet pages handle connection and
payment confirmation. The terminal and local `/chat` API use the same chat handler.

## Setup

```sh
uv sync --frozen
npm ci
uv run agent web3-init
```

`web3-init` initializes the local encryption vault. It does not create a separate
Arkiv wallet. Create a wallet through `/wallet create`, or connect an existing one
through `/wallet connect`. The selected storage wallet in that chat owns new Arkiv checkpoints.
Use `/arkiv wallet agent` for automatic storage from the agent-created wallet,
`/arkiv wallet connected` for MetaMask storage, or `/arkiv wallet active` for the
default wallet selection. This preference is per chat and applies to new tasks.
MetaMask can remain the payment source while the agent wallet pays for storage.

Fund that same address with **tGLM on Rabdi** for task storage. For payments, fund
the same address with **ETH on Sepolia**. These balances are independent. Inference
configuration and billing remain separate. The generated accounts are ordinary
key-controlled accounts, not deployed smart contract wallets. Rabdi native entity
writes currently require that account type.

Agent-created wallets sign checkpoints automatically with their existing encrypted
key. MetaMask wallets sign each checkpoint through a short-lived link in the chat.
The browser selects Rabdi, presents the network fee, and admits only one signing
request per checkpoint. The server verifies the transaction, receipt and entity
readback. Work waits at checkpoint barriers before external effects can execute.
A connection signature alone never authorizes storage transactions.

`/arkiv` reports the selected wallet, live tGLM balance, query scope and any pending
signing link. Wallet addresses are selected from authenticated chat identity and
wallet files, never a hardcoded address. Changing the active wallet applies to new
tasks; existing tasks keep their original identity. Reconnect their original wallet
to update them. Existing application-writer checkpoints remain readable but are
not silently copied to a new wallet.

Configure `AGENT_ARKIV_ACCESS_KEY` locally in `.env` using a key from
[Arkiv Hub](https://hub.arkiv.network/access-keys). Empty uses Rabdi's limited public
RPC. SDK **1.0.1**, Rabdi **7794095**. `AGENT_ARKIV_MAX_FEE_WEI` caps each checkpoint
fee at 0.01 tGLM by default; actual fees depend on the exact unsigned estimate.
A process-wide writer lock serializes this installation, while separate per-address
journals preserve uncertain writes and external signing requests.

`AGENT_TASK_BACKEND=arkiv` is the default. `sqlite` is an explicit offline mode
for unrelated agent development; **payments are disabled there**.

## Use the existing Ira WhatsApp sidecar

The adapter uses the existing protocol inspected in the reference source:

- Inbound JSON in the `data` field of Redis stream `ira:wa:in`.
- A line session named `line-…`, with message/chat ID and `sender_lid` identity.
- Outbound `POST /send/{line}` with `{to, text}` and Bearer authentication.

Set `AGENT_WHATSAPP_SIDECAR_URL`, `AGENT_WHATSAPP_SECRET`,
`AGENT_WHATSAPP_REDIS_URL`, `AGENT_WHATSAPP_LINE`, and
`AGENT_WHATSAPP_ALLOWED_CHATS` in `.env`. The allowlist contains exact chat JIDs,
for example `COUNTRY_CODE_AND_PHONE@s.whatsapp.net`, not wallet addresses.
Use an already paired **dedicated hackathon line**. The production Ira agent and
this agent must not both answer the same line/chat. This code does not pair a phone,
modify production routing, or import the reference application.

```sh
uv run agent whatsapp
```

The new Redis consumer group is `ira-web3-agent`; it does not acknowledge messages
in production's `ira-api` group. A newly created group begins with new messages.
Its own pending entries are inspected at restart. The adapter accepts direct text
messages on the configured line and chat allowlist. It ignores groups, history,
forwarded text, edits and media. **Payment approval must be typed text**, not a
voice transcription or quoted message. Sender LID is required; a phone-number
change must not grant access to a previous account's wallet.

No sidecar credentials are supplied in this repository. Running the process and
having a live WhatsApp session are separate setup steps.

## Chat flow

Natural-language tasks use the model and its wallet tools. These exact commands
also work without model interpretation:

```text
/wallet create
/balance
/balance 0x66051Eef2e7DD1b410C6E68EeDF8ABBddB783Ee1
/transfer 0.001 0xRECIPIENT_ADDRESS
```

Wallet creation returns a new public payment address. Fund that address with
Sepolia ETH from MetaMask or a faucet. It does not move funds from MetaMask for you.
`/wallet connect ADDRESS` watches an existing address for reads only; an explicit
`/wallet create` may replace a watch-only entry, but never replaces a signing wallet.

Preparing a payment returns recipient, sender, amount, network, maximum gas fee,
expiry and a random code. The user then replies:

```text
approve CODE
reject CODE
/receipt TRANSFER_ID
/tasks
/task TASK_ID
/arkiv
/recover
```

Approval is recognized from the original incoming message **before inference**.
It writes a user-origin approval event bound to the exact frozen transaction.
The signer checks that event. No model tool can approve or broadcast a transfer.
Codes expire after five minutes. A code from another chat/user cannot authorize it.
Repeated approval returns the already recorded outcome. One task has one payment
intent; changing recipient or amount requires new explicit work and approval.

Direct payments are native **ETH on Ethereum Sepolia (11155111)**, to addresses without
contract code. The default direct-transfer limit is 0.01 ETH.
CCIP-BnM transfers from Ethereum Sepolia to Base Sepolia are also supported through
the existing authorization flow, using native ETH fees. See [CCIP.md](CCIP.md)
for deployment, test tokens and delivery verification. Swaps and mainnet payments remain unsupported.
The server stores each signed transaction and hash in an encrypted Arkiv checkpoint
before provider admission. It never automatically resends after a lost response.

The worker checks pending receipts every 15 seconds while running. A matching
transaction, successful canonical receipt and at least two confirmations are required
for `confirmed_transfer_receipt` evidence. Two confirmations are a demo policy,
not an assertion of irreversible Ethereum finality. The model cannot complete a
payment task using only a balance read or its own claim. A reverted transaction
remains failed. The confirmed task summary is sent through the same WhatsApp line.

## What depends on Arkiv

`/tasks`, `/task`, the model's task list and `GET /tasks` query Arkiv. They do not
return SQLite results if Arkiv fails. At startup, before a worker claims tasks,
Arkiv checkpoints restore task instructions, notes, waits, task messages, evidence,
effect receipts and payment references into SQLite. Interrupted effects become
unknown. A submitted payment resumes receipt checks, never another send.

Checkpoints are encrypted immutable entities. Queries pin the configured creator
and current owner, validate readonly creation, and filter the named schema plus
instance. The newest revision per task is selected. Old or foreign publishers do
not become trusted just by copying an application tag.

`/recover` reconciles an admitted Arkiv write using its saved hash, then reloads
checkpoints. It refuses recovery while task jobs are active. A failed write stops
the affected task until recovery/restart succeeds; other wallets do not inherit its failure. The bridge never automatically
retries an uncertain or reverted write. A prepared journal with no admitted hash
requires operator inspection; deleting it is not a supported recovery method.

## Keys, retention and recovery limits

Back up these private local files through your normal secure backup process:

- `.local/vault.key`: encryption key for task snapshots and signing-key files.
- `.local/arkiv-scopes.json`: chat/wallet scopes and task-to-wallet bindings.
- `.local/arkiv-wallet-*.json`: each chat's storage-wallet selection.
- `.local/metamask-*.json`: signed wallet connections, with no private keys.
- `.local/wallet-*.json`: each agent wallet's encrypted signing key and address.
- `.local/arkiv-send-*.json`: per-wallet admitted transactions and pending checkpoints.
- Legacy `.local/arkiv-writer.json` and `.local/arkiv-send.json`, if present: preserve for old records.
- `.local/whatsapp.sqlite3`: inbound deduplication, routes and outbound receipts.

Signing keys are never published to Arkiv, sent to the model, or printed in chat.
Anyone with the local vault key **and** its encrypted wallet files can sign; this
is server-held wallet custody, not an externally protected hardware wallet.
Deleting SQLite alone can be recovered without losing signing custody. Losing the
vault key prevents decryption; losing the wallet-key files loses future signing
access. The application does not invent a replacement wallet on recovery.

Each checkpoint lasts 30 days. While running and funded, the worker prepares a
fresh checkpoint for unfinished work at least weekly. Agent-created wallets sign
it automatically; connected MetaMask wallets require approval before it is published. Completed/cancelled task
history expires 30 days after its last write. If the app remains offline beyond
retention, the key alone cannot restore expired records. Expiration does not cancel
a submitted payment. A Rabdi reset can erase testnet data; no automatic reseeding
or resend is attempted.

Checkpoint plaintext is capped at 85 KB, queries at 5,000 revisions per scan.
Exceeding either bound stops work rather than silently truncating recovery. The
SDK bridge currently runs synchronously between Python steps. Checkpoint writes
therefore add noticeable latency and gas cost; this is a single-process demo,
not a horizontally scaled service.

WhatsApp send admission is saved before calling the sidecar. Its successful HTTP
receipt means accepted by the sidecar, not delivered/read on a handset. An uncertain
send is retained without automatic replay. A crashed inbound turn is not blindly
re-executed; the user can ask for tasks/receipts. These choices can require manual
follow-up after interruption and need handset testing.

The local HTTP API has no production identity verification. Keep it on loopback.
Only the WhatsApp adapter should derive a WhatsApp user's identity from its trusted
sidecar stream. Do not expose `/chat` as a public webhook accepting arbitrary user IDs.

## Independent read and demonstration

Get the active wallet and scope from `/arkiv`, then stop the app if a
stable multi-page observation is needed. Run without the application API:

```sh
node --env-file=.env integrations/read-tasks.mjs WRITER_ADDRESS INSTANCE_ID
```

This performs a real typed Arkiv query and prints public checkpoint metadata.
The encrypted bytes are read by the SDK; the independent example deliberately
prints no private task text. The application decrypts them using its local vault.

For a live demo: create and fund an agent wallet; ask Ira to prepare a payment;
inspect `/tasks`; approve in chat; restart with a backed-up SQLite cache removed;
observe the saved transaction being checked; receive its confirmed receipt in chat;
and run the independent reader. Preserve all private recovery files when removing
a test cache. Do not remove production data to demonstrate recovery.

Automated fixtures exercise the actual signing/SDK code and simulated provider
responses. Live checks so far establish public Rabdi/Sepolia reads and Mumbai MCP
connectivity. They do **not** establish an on-chain checkpoint write, live transfer,
WhatsApp delivery or handset experience. Those require configured/funded wallets
and the dedicated WhatsApp line.


## MetaMask Mobile connection and signing

The user can keep funds in an existing MetaMask wallet. Configure a dedicated
HTTPS origin in `AGENT_WALLET_PUBLIC_URL`, backed by the wallet-only service on
`AGENT_WALLET_PORT` (8765 by default). `agent whatsapp` runs that service alongside
the worker. The public service has no `/chat`, `/tasks`, general event API, or API
documentation. Keep the general agent API private.

For a local demo, `cloudflared tunnel --url http://127.0.0.1:8765 --protocol http2`
can supply a temporary HTTPS origin. Set the returned origin in `.env` and restart
the agent before issuing links. A tunnel restart can change that address; old links
will then stop working. This is a demo transport, not a production hosting setup.

Send `/wallet connect` to receive a link that opens the setup page in MetaMask's
mobile browser. The app selects Ethereum Sepolia, requests fresh account access
with `wallet_requestPermissions`, then reads the permitted account again before
requesting a connection signature. This order matters: MetaMask Mobile can return
an account from `eth_requestAccounts` while its signing permission is missing on
the selected network. The app validates the address and Sepolia chain before
building the sign-in message. The signature
binds a fresh nonce, origin, account and expiry to the existing WhatsApp identity.
The user selects an account in MetaMask and proves control with a signed connection message.
There is no installation-wide pinned wallet address.
Connection links last ten minutes and cannot be used to change the account after
completion. The backend verifies the signature; account-connection approval alone
is not a completed setup.

A connected MetaMask wallet is preferred for subsequent balance reads and payment
preparation. The original agent-wallet key file is preserved. No MetaMask private
key enters the backend. Connecting does **not** grant spending permission or
implement Advanced Permissions / background delegation.

Payment requests still begin in chat. `approve CODE` publishes the approved intent
and returns a fresh confirmation link. The user opens it, reviews the frozen payment,
switches to Sepolia if needed, and signs in MetaMask. Before returning a signable
transaction, the server saves one-shot admission and an Arkiv checkpoint. A second
link cannot create another signing request for that payment. The wallet page sends
the resulting hash back; the backend verifies the exact sender, recipient, amount
and nonce before accepting it. The task then follows the existing receipt checks.

A lost browser response is recovered from the saved hash, or by finding the approved
sender/nonce in a bounded 500-block scan. It never starts another signing request.
The page also lets the user save the existing hash from MetaMask activity without
sending again. A declined or uncertain wallet request remains for inspection;
automatic retry is deliberately absent.

Preserve `.local/wallet-links.sqlite3` for admitted mobile requests and
`.local/metamask-*.json` for external-wallet preferences when testing task-cache loss.
These are separate from signing-key backups and from the disposable task cache.
Only bounded connection-stage names and RPC error codes are recorded for debugging;
private keys, signature payloads and arbitrary browser logs are not collected.

The WhatsApp line has been linked and the user confirmed receiving the wallet-create
reply on the handset. The MetaMask page and backend were tested, but mobile connection
and payment success must be established by a completed handset signature and matching
server result, not by seeing a MetaMask connection prompt.

## Payment routing and unsigned reviews

Natural-language payments now use `prepare_payment` in the chat before creating
any storage transaction. It takes explicit asset, amount, source network,
destination network and recipient fields. Unsupported routes and a missing CCIP
sender deployment return a setup error directly, including when an unrelated task
already has a pending Rabdi storage approval. They never substitute that storage
link for the requested payment.

A valid request creates an unsigned local review paused for the exact approval
code. This draft is not yet an Arkiv-confirmed task. The approval path still saves
the exact authorized intent on Arkiv before admitting a financial signature. The
MetaMask signing request itself is also saved in an encrypted `payment-*.json`
journal under `.local/`, before it is returned to the wallet. This journal survives
SQLite recovery; it prevents re-admitting a wallet request after an interrupted
response. Preserve these files with the existing vault and wallet-link database.
Wallet admission and hash attachment do not request another storage signature.

Expired unsigned reviews can be refreshed on the same payment and task using
`refresh_payment_review`. This rotates the approval code. A claimed or submitted
request cannot be refreshed or sent again. Signing remains a user action; neither
routing nor local draft creation authorizes a transfer.
