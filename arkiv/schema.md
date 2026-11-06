# Task checkpoint schema v1

SDK: `@arkiv-network/sdk` 1.0.1. Network: Rabdi, chain 7794095.
Named schema: `ira_task_checkpoint_v1`.

| Column | Type | Indexed | Meaning |
|---|---|---|---|
| `instance` | str | yes | Opaque chat/wallet scope, derived using the local vault key |
| `task` | str | yes | Random task identifier |
| `revision` | u64 | yes | Monotonically increasing application microsecond revision |
| `operation` | str | yes | Digest binding this exact checkpoint write |
| `encrypted` | bytes | no | AES-256-GCM encrypted JSON snapshot, stored as UTF-8 base64 envelope bytes |

The named schema also adds the SDK's `arkiv.schema` tag. Queries filter the
independently configured `$creator` and `$owner`; tag matching alone is not trust.
Readonly creation is required. The latest revision per task is the current state.

The encrypted JSON contains version, task fields, task journal/evidence, task model
messages, effect receipts and payment intents/status/hash. It contains **no wallet
signing keys**, RPC access keys or inference credentials. AES-GCM additional
authenticated data binds instance, task ID and revision. Each encryption uses a
fresh random 12-byte nonce. Ciphertext length, indexed attributes, creator/owner
and expiry are public. The bytes column being unindexed does not make it private.

New entities are created and owned by the active wallet selected in that chat.
The scope binds chat identity and wallet address using the local encryption key.
Switching wallets does not reassign existing tasks. Legacy application-writer
records retain their original owner and remain readable.
Retention is 30 days per checkpoint. Unfinished work is refreshed weekly while
running and funded; MetaMask refreshes require user approval. Completed history ages out. See docs/WEB3.md for limits.
