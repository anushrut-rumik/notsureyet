# OpenAI inference

Chat, planning, and workers all use `gpt-6-luna` at
`https://api.openai.com/v1`. Their routes are configured in `inference.toml`.
The client uses OpenAI's Responses API for reasoning with function calls.
There is no automatic fallback to another inference provider or model.

## Credentials

Set `OPENAI_API_KEY` in the project's ignored `.env` or process environment.
The environment takes precedence. Keep credentials server-side and out of source
control. If a key has been shared in chat, revoke it and replace the local value.
`uv run agent config` reports key presence without printing its value.

## Request and tool handling

Each request sends the conversation and the tools available to that agent role.
The application executes returned function calls and sends their results back with
the matching call IDs. Encrypted reasoning items from the same endpoint and model
are preserved between turns. State from a different model is not replayed as native
reasoning; its visible messages and tool results remain available.

Requests set `store=false` and request `reasoning.encrypted_content` so the
application can carry reasoning state itself. This setting does not replace
OpenAI's account-level data policies. Chat and task history are still persisted
by the application.

General and worker endpoints allow 8,192 output tokens; planning allows 16,384.
The model's default reasoning effort is used. These budgets include reasoning
tokens. Incomplete or empty responses are rejected before any tool is executed.

Token usage and response/request identifiers are recorded in `inference_usage`
events, outside the model conversation. Provider error bodies are not logged.
The SDK makes no automatic retries. The existing chat/worker retry policy handles
transient failures and honors provider delays. Invalid credentials, missing model
access, and exhausted quota require intervention.

## Verification

```sh
uv run agent config
uv run agent doctor
uv run pytest
```

`doctor` reads the model catalog to check credentials and model visibility. It does
not prove that paid inference or tools work. `uv run agent doctor --live` makes two
paid requests per role: one function call, followed by a reply that must use the
returned random token.

The regular test suite uses simulated responses and does not call paid inference.
It covers reasoning replay, tool results, usage reporting, incomplete output,
safe errors, quota handling, and recovery. To run the opt-in end-to-end tests:

```sh
AGENT_RUN_LIVE_TESTS=1 uv run pytest tests/test_openai_live.py -q
```

Those tests use an isolated SQLite workspace to verify background task handoff,
file creation, read-back, completion evidence, and a question/resume exchange.
They do not establish live Arkiv, payments, or WhatsApp behavior.

## Official references

- [GPT-6 Luna model](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [Responses API migration](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
