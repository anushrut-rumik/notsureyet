# Encrypted lending activity on Arkiv

The activity mirror publishes a history of the existing lending application. It
does not execute CRE, authorize transfers, change an approved plan, or replace
the execution journals. The installed Arkiv SDK is 1.0.1 on Rabdi (7794095).

## What is captured

- Each committed `lending_private` state revision, including setup, approval,
  evaluation, pause, expiry, failure and withdrawal state.
- Each structured `lending_update` event, even when no chat notification is sent.
- Every existing operation-journal history row, including uncertain submission,
  source confirmation, in-transit state, destination verification and recovery.
- Initial snapshots and every subsequent insert/update in the setup `actions`
  and live wallet `txs` journals. Raw signed transactions and calldata are excluded.

SQLite triggers copy state and transaction revisions locally, so several changes
between observer polls remain separate records. The original state is already
encrypted. Operation histories and structured updates are read from their
existing durable tables. The observer closes all source connections before it
makes an Arkiv request. It scans every five seconds while running.

The initial backfill can include only facts the application previously retained.
It cannot reconstruct overwritten historical management states or lost logs.
Initial snapshots are labelled as snapshots, not invented historical events.
This is an application activity log, not a trace of every internal RPC request
or model token. A journal state is attributed to the application. A successful
storage transaction does not prove a transfer or destination deposit succeeded.

## Encryption and lookup

`ira_lending_activity_encrypted_v1` has only `scope`, `event` and `encrypted`
columns. The first two are keyed opaque identifiers. Amounts, activity types,
decisions, execution hashes and timestamps are inside the encrypted column.
Publisher identity, publication timing, record count and ciphertext length remain
public. Chat messages, chat identifiers, keys, raw transactions and signatures
are excluded from the payload.

Records are losslessly compressed before encryption. The existing local Vault
uses AES-256-GCM with fresh random nonces. Each payload
is authenticated against the activity namespace, Rabdi chain ID, scope and event
ID. The decryption key stays local and is independent of the wallet signing key.
Back up the existing `.local/vault.key` privately. Losing it loses decryption.
Large records are split into numbered encrypted parts without dropping content.

The authenticated `lending_activity_history` agent tool reads the user's local
mirror quickly, or fetches and decrypts records directly from Arkiv with
`verify_on_arkiv=true`. Remote reads require the configured creator, owner and
readonly creation flags before decryption. Plaintext is data, never tool authority.
The Arkiv explorer shows encrypted records; the local timeline shows decrypted
details and links to verified storage transactions.

## Upload and recovery

The uploader runs in a separate process. It uses the existing encrypted local
agent wallet and the configured `AGENT_ARKIV_MAX_FEE_WEI` storage cap, defaulting
to 0.01 tGLM per batch. Exact unsigned estimates, current fees, balance, nonce,
and half-block gas limits are checked before submission. Batches hold up to 20
records with an additional payload-size bound and
use one native atomic transaction each. No new dependencies are installed.

The importer freezes and hashes each plan, saves signed bytes/hash before the
first provider send, verifies canonical receipts and the exact encrypted entity
fields, then marks records verified. A timeout remains pending. It reconciles
the original hash instead of submitting a replacement. Process restarts retain
both the outbox and the original send journal.

The publisher, existing Arkiv task writes and Rabdi payment admission share a
single-host signer lock. A pending activity transaction reserves that Rabdi
signer until reconciled. This can delay another Rabdi storage/payment operation.
It does not reserve the Sepolia/Base Sepolia signer nonces or the lending lock.
There is no Arkiv request or confirmation wait inside either CRE workflow.

Entities have a 30-day lifetime. This version does not automatically extend
expiration. Verified counts mean successful upload/readback at sync time, not
permanent availability. Local history and encryption-key backups remain necessary.

## Commands

Run from the project root:

```sh
uv run python -m agent.lending_activity collect
uv run python -m agent.lending_activity sync
uv run python -m agent.lending_activity watch
uv run python -m agent.lending_activity status
uv run python -m agent.lending_activity read --user USER_ID --remote
```

`collect` installs the local capture triggers and performs no network writes.
`sync` publishes one batch per user. `watch` collects and uploads repeatedly,
draining a backlog without the normal five-second idle delay.
Only one collecting/publishing process can hold `worker.lock`. Other processes
can read status. The local report is `.local/lending-activity/timeline.html`.
Stopping the observer pauses uploads while installed capture triggers and the
operation journal continue preserving local history.

## Validation

Tests cover all intermediate saved revisions, rollback, repeat collection,
encrypted user isolation, tamper/wrong-context rejection, source transaction
redaction, continued state updates during a storage outage, signer coordination,
readonly publisher checks, and a lost raw-send response without a second send.
The existing CRE and lending regressions are run separately. Live network proof
must come from the actual confirmed entities and their decrypted byte readback.
