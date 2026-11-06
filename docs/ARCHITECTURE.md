# Agent architecture

## The core idea

A chat turn is short. A job can outlive it. The agent therefore stores a task with
the user's actual instruction, an observable completion condition, working notes,
and a journal. A background worker keeps running that task until it finishes, waits,
needs the user's input, or is cancelled. Task state survives process restarts.

The implementation is independent Python code. Each task run yields after a
configurable number of steps, saves its progress, and queues the next run. That
keeps the local worker responsive while preserving unfinished work.

## Components and data flow

1. `cli.py`, `server.py` or `whatsapp.py` receives a message and calls `Runtime.chat`.
   `chat_commands.py` processes exact user approvals before inference; there is no model signing tool.
2. `engine.py` records it, includes memories and current tasks, and runs the model loop.
3. `providers.py` selects the configured endpoint for chat, planning, or worker inference.
4. `tools.py` validates arguments, records effects, executes the actual tool, and journals its result.
5. A task has a concrete `done_when`. The background worker in `runtime.py` claims it
   and runs the same loop with task tools.
6. `arkiv.py` encrypts task checkpoints and uses the official SDK bridge in `integrations/arkiv.mjs`
   to publish/query immutable Arkiv records. `store.py` supplies the local execution cache and private chat/preferences.
7. `wallet.py` selects each chat's active wallet for payments and Arkiv storage. Agent-created wallets
   keep encrypted keys; connected MetaMask wallets approve each checkpoint on Rabdi. The bridge
   derives a separate scope per chat/wallet and pins existing tasks to their original wallet.
   Storage barriers wait for confirmed readback before external effects run. Wallet preparation, user-event authorization,
   signing, admission journals and receipt checks. Recovery never requires another payment send.

All inference roles use OpenAI `gpt-6-luna` through the Responses API. The client
preserves encrypted reasoning items and tool-call identifiers through tool turns,
rejects incomplete output before executing tools, and records token usage separately
from the model conversation. Requests use `store=false`; conversation and task state
remain in the application's own storage. Chat hands a newly created task to the worker
immediately. See [OpenAI inference setup](OPENAI.md) for configuration and validation.

No model output is treated as proof that an external action happened. Task completion
must cite existing successful evidence events from that same user's task. This checks
the source of evidence; the model still judges whether it proves `done_when`.

## Task states

`queued` → `running` → `completed`, `waiting`, `needs_input`, or `cancelled`.

A run that uses its step allowance returns to `queued`. A timed wait resumes when
due. A user answer resumes a task through `reply_to_task` or the HTTP reply endpoint.
An interrupted running task is queued at startup. A pending action from an interrupted
process becomes `unknown`, so it cannot be blindly replayed under the same action key.
The agent can inspect reality and settle it with recorded evidence.

Transient inference failures are recorded and retried with persisted waits that
honor the provider's retry interval. Configuration, authentication, and payment
errors pause immediately. Three failed runs also pause for intervention. The task
is not marked completed and its progress is retained.

## Persistence and effects

SQLite holds tasks, events, conversation messages, memories, effects, public wallet
references and payment intents. Arkiv is authoritative for tasks; startup restores
their messages, evidence and payment references before claiming work. There is one local process per database. SQLite transactions claim work
and persist writes. The process uses asynchronous jobs for independent tasks.

Action keys include the task or chat turn, the tool name, and its arguments. A repeated
successful action with that key returns its stored receipt. This helps avoid duplicate
actions on recovery. It is not a distributed exactly-once guarantee: an integration
should also use its provider's own idempotency key when available.

Memory writes cite the user's message. Tools that read pages or files cannot write a
standing instruction directly. `forget` removes a memory entry; it deliberately does
not claim to erase the original conversation or every derived file.

## Extending it

Add tools in a module exposing `register_tools(registry)`, then list the module in
`AGENT_TOOL_MODULES`. An async function's annotations become its tool schema. The
function receives `ctx`, which provides the user, task, store, and engine.

- `effect=True`: record before an action and keep its result for recovery.
- `evidence=True`: successful output can serve as completion evidence.
- `modes=("task",)`: offer a tool only to background task sessions.

The included `examples/my_tools.py` adds a real file word-count tool. New email,
calendar, browser, or business-system tools fit the same interface. Their credentials
belong in your own environment, separate from inference keys.

## Deliberate boundaries

- SQLite is a local execution cache. Arkiv supplies task authority and recovery.
- WhatsApp mode uses the existing Ira Redis stream and sidecar API; terminal mode needs no Redis.
- Memory is explicit and source-linked; the production daily consolidation system is not ported.
- Timers run only while the application is running. Overdue work resumes at startup.
- File tools work inside `workspace/`; there is no general shell tool by default.
- Web fetching reads pages. It does not click through websites, sign in, or book anything.
- No live Gmail, telephone, mobile device or production accounts are connected.
- The WhatsApp adapter is implemented for an existing dedicated sidecar line; its credentials are not configured.
- The HTTP server binds to loopback and has no production authentication.
- Run either terminal mode or server mode for a database, rather than starting both workers.
- Keys are not bundled. Provider tests exercise the OpenAI tool loop;
  production-level task reliability still requires broader evaluation.

These boundaries keep the code small enough to change. The task loop, persistent
progress, actual tool execution, and evidence-linked completion remain the foundation.

See [Web3 chat flow and verification limits](WEB3.md) for payment authorization,
Arkiv ownership, retention, recovery, signer custody and WhatsApp configuration.
