# Ira task schema v2

Network: **Rabdi (7794095)**. SDK: **@arkiv-network/sdk 1.0.1**.
Definitions: [task-state.mjs](../integrations/task-state.mjs).
Encryption/recovery: [arkiv.py](../agent/arkiv.py), [vault.py](../agent/vault.py).

## Entities, ownership and retention

| Schema | Purpose | Editing rights | Lifetime |
| --- | --- | --- | --- |
| ira_task_current_v2 | One current task record with encrypted current snapshot | Selected storage wallet can patch | Active: 30 days; completed/cancelled: 180 days |
| ira_task_checkpoint_v2 | Immutable versions verifying current state and rebuilding execution | Readonly contents; owner can extend, transfer or delete | Active: 30 days; terminal: 180 days |
| ira_task_checkpoint_v1 | Earlier encrypted checkpoints, readable for compatibility | Readonly contents; original owner retains other rights | Original 30 days |

Creator and initial owner are the chat's selected storage wallet, not Arkiv or the
model. Selecting an agent wallet for storage does not change the connected payment
wallet. Switching wallets never silently reassigns existing tasks. Actual writer
addresses and queries are in README.

## Fields

V2 schemas share these attributes. All strings are indexed; revision is indexed
u64; encrypted is unindexed bytes.

| Field | Meaning and query use |
| --- | --- |
| instance | Opaque chat/wallet scope derived using the vault key; every application query uses it |
| task | Stable task ID; progress and recovery lookup |
| revision | Increasing application version; stale-update guard |
| operation | Digest of scope, task, version, ciphertext and public metadata; write reconciliation |
| status | queued, running, waiting, needs_input, completed, cancelled |
| capability | task, research, file, reminder, payment, simulation; deployment for the sender-setup task |
| protocol | ira, native, ccip, chainlink_local |
| source_network / destination_network | Execution networks; simulations explicitly use local_evm |
| reference | Payment intent or saved simulation ID; empty for ordinary tasks |
| source_tx / destination_tx | Actual transaction hashes when known; otherwise empty |
| order_id | CCIP message ID once observed, otherwise its intent ID; simulation IDs stay under chainlink_local |
| progress | Protocol/payment status or ordinary task status; never private notes |
| encrypted | AES-256-GCM encrypted task fields, journal/evidence, task messages, effect receipts and payment records |

V1 has instance, task, revision, operation and encrypted only. The SDK adds an indexed
arkiv.schema tag. Creator, owner, key, expiry and creation flags are Arkiv metadata.
Public SDK clients can filter indexed v2 fields. Application status/protocol filters
run after resolving current state.

## Current state and stale-checkpoint protection

One atomic native batch creates the first current record and a readonly checkpoint.
Later batches create a checkpoint, patch that same current record and extend its
lifetime together. Current contents must match the immutable checkpoint. Python
also checks public fields against their authenticated copy inside the ciphertext.

A retained terminal checkpoint wins over old/reopened current state. Missing,
duplicate, conflicting or mismatched active records stop recovery. V2 supersedes
V1 for the same task; disagreement never causes fallback to older history.
The normal task API cannot reopen terminal tasks. Mutable owner/creator matching
alone is insufficient: the readonly counterpart is required.

This supports one coordinated writer per wallet. Native patching has no
compare-and-set. Local locks, expected revisions and fresh nonce checks do not
coordinate machines. Owner deletion/transfer, withheld signatures and testnet resets
can make data unavailable; none authorizes reissuing a payment.

## Public metadata, encryption and key recovery

Every indexed field, address, identifier, expiry and ciphertext size is public.
Transaction/message IDs can correlate tasks with public chains. Titles,
instructions, notes, detailed results and task messages are encrypted. Unindexed
bytes remain public ciphertext.

AES-GCM uses a fresh 12-byte nonce and authenticates instance, task and revision.
Signing keys, provider credentials and RPC keys are not checkpoint fields.

Recover with the original .local/vault.key and trusted writer/instance descriptors
from .local/arkiv-scopes.json. An address or signature cannot replace the encryption
key. A clean observer restores tasks without wallet signing files or payment
journals. New publication still needs storage-wallet authorization. The vault key
plus encrypted wallet files grants wallet-key access: keep both outside the repo.

## Expiration behavior

A running, funded worker refreshes unfinished work weekly; MetaMask users approve
each storage transaction. Terminal records last 180 days, longer than earlier
active/V1 versions, so normal expiry cannot expose old runnable work. Terminal tasks
are not refreshed indefinitely. Current-record extensions and new checkpoints use
the same requested duration.

Expiry is block-based, not guaranteed wall-clock time. Keys cannot restore expired
or reset data. Expiry does not erase copies or cancel payments. Guarantees concern
retained records written through this lifecycle, not malicious out-of-app writes.
