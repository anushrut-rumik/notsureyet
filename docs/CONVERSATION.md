# Conversational work

Users can ask for work in ordinary language. Slash commands remain shortcuts.
The assistant can perform quick tool actions in a chat turn. Multi-step work,
verified file creation, payments and reminders use persistent background tasks.

## Message flow

1. Runtime receives the actual user message and recovers saved work.
2. Engine takes a per-user lock and saves the incoming message in chat history.
3. Exact command and payment approval handling runs before model inference. Its
   reply is saved in the same history as ordinary conversation.
4. Other messages enter the model/tool loop. Recent channel messages supply
   background questions, results and command replies, with their task IDs.
5. The model performs a quick action or saves a task with its full instructions.
   After task creation, a separate reply without action tools acknowledges the
   request. If that reply fails, a fixed acknowledgement preserves the saved task.
6. The background worker can ask a question, wait, or finish with recorded evidence.
   The next chat turn can route an answer or correction back to the same task.

The model decides which task a reply concerns. If multiple tasks could match, its
instructions require clarification. This is not a deterministic guarantee of intent
recognition. Tool ownership checks still prevent accessing another user's task.

## Arkiv remains authoritative

A chat prompt uses a bounded local execution snapshot to help route replies.
It is explicitly labelled as context, not authoritative status. Constructing a
prompt does not query Arkiv. `list_tasks`, `/tasks`, checkpoints, and task recovery
retain the existing Arkiv behavior. A failed remote task query is a failed tool
result; it is not replaced with an empty list or local task status.

Task execution requires successful recovery. The WhatsApp and wallet interfaces
can stay available while Arkiv recovery is waiting for its retry window. Durable
checkpoints are still required before saved work executes.

## Errors and retries

A transient model failure can retry once in chat, with a delay of at most three
seconds. Longer provider retry intervals are not shortened. The retry covers only
the model request, so tools and payments already executed are not repeated.
Background tasks retain their existing persisted retry policy.

Each chat model request has a 45-second deadline, configured by
`AGENT_CHAT_INFERENCE_TIMEOUT_SECONDS`. A deadline does not repeat the tool loop.
WhatsApp records a progress update after 15 seconds if the turn is still running,
configured by `AGENT_WHATSAPP_PROGRESS_SECONDS`.

`chat_error` events record exception type and safe inference codes/request IDs.
They exclude arbitrary exception text and provider bodies. WhatsApp replies now
distinguish model errors, task storage failures and unexpected internal errors.
A generic greeting failure no longer recommends payment commands.

Error reporting is best effort if SQLite or the disk itself has failed. Failure to
save an error reply does not stop the receiver. The durable inbox preserves that
input as interrupted; after storage recovers, a notice explains the interruption.
Restart recovery never replays its original tools or payment request. Recovery
notices use the input identity to avoid generating a second notice after a crash.

## Verification

Regression tests cover natural wallet creation, command follow-ups, background
questions and corrections, completion evidence, user separation, unavailable task
queries, acknowledgement failure, bounded inference retry, and simulated WhatsApp
inbound/outbound delivery. Wallet and transfer tests retain raw-message approval
checks and prevention of duplicate sends.

Real model checks use temporary SQLite databases, temporary test wallets and test
files. They do not establish live Arkiv writes, WhatsApp handset delivery, or live
payments. External services such as email, calendar and browser interaction still
need their own registered tools and account connections.
