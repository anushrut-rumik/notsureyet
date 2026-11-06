# How Ira uses Arkiv

## Do filters, ownership and expiry fit the product?

Task IDs and chat/wallet scopes select a user's work. Indexed status, capability,
protocol, networks and transaction/order references expose current progress.
/tasks, /task ID, list_tasks, task_status, task_journal, GET /tasks and GET /tasks/ID
read Arkiv. HTTP listing accepts status/protocol filters after current resolution.
SQLite is the execution cache, never a silent fallback for unavailable Arkiv.

One lifecycle covers research, files, reminders, native payments, CCIP and archived
Chainlink Local simulations. Payment intent, source transaction, CCIP message and
destination transaction remain distinct. Simulations use chainlink_local/local_evm
and never prove public delivery.

The selected wallet owns records. Agent wallets sign storage automatically;
MetaMask storage needs confirmation. Atomic batches update current state and create
readonly encrypted checkpoints. Terminal markers prevent old active versions
reviving work. Active records last 30 days with weekly refresh; terminal records last
180 days. [Full schema](schema.md).

## Does each feature change what users see?

| Feature | User-visible behavior | Evidence |
| --- | --- | --- |
| Indexed reads | Lists/progress show saved status and protocol references | agent/arkiv.py, agent/chat_commands.py, agent/server.py |
| Atomic batches | No current update without its checkpoint | integrations/arkiv.mjs, integrations/task-state.mjs; demo.md transaction links |
| Readonly history | Replayed running state cannot reopen completed work | tests/arkiv-current.test.mjs, tests/test_arkiv_current.py |
| Encrypted recovery | Fresh Ira reconstructs task details and payment identifiers | scripts/recover-arkiv-task.py; evidence/live-recovery.json |
| Wallet ownership | Storage custody can differ from payment custody | tests/test_arkiv_wallets.py |
| Explicit expiry | Active refresh; longer bounded terminal retention | schema.md; agent/arkiv.py maintain() |

## Reproduce

Use README's actual writer/scope with integrations/read-tasks.mjs. Run npm test and
uv run pytest -q tests/test_arkiv_current.py tests/test_arkiv_wallets.py.
The independent reader uses the SDK without our API or encryption/signing keys.
The recovery script creates a new directory and an Ira Runtime with inference and
mutations disabled, using explicitly authorized local key/scope access.

Unsigned estimates, gas/fee caps, nonce checks, durable signed bytes/hash journals,
receipts and readback still gate writes. Uncertain submissions are reconciled,
never resent. Storage does not grant spending authority: exact raw-chat approval
and wallet confirmation retain their roles. [Demo and limits](demo.md).
