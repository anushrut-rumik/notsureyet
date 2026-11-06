# Local Ira WhatsApp line

This compose project runs the existing Ira `ira/wa` implementation from the local
reference checkout. It has its own PostgreSQL key store, Redis and event stream.
It does not connect to the production Ira database, Redis or phone sessions.

Private settings are in `.local/whatsapp.env` (sidecar and database) and `.env`
(agent). Both are ignored by Git. The phone number in `AGENT_WHATSAPP_PHONE` is
the agent's number. `AGENT_WHATSAPP_ALLOWED_CHATS` is the separate test sender.

```sh
docker compose -f integrations/whatsapp/compose.yml up -d --build
uv run agent whatsapp-link
uv run agent whatsapp-status
uv run agent whatsapp
```

Enter the returned pairing code on the agent phone in WhatsApp's Linked devices
flow. `whatsapp-status` must show `connected: true` and the intended agent number.
Then send `/wallet create` from the allowed test phone to that agent number.

To restart the Python task worker, stop and start `agent whatsapp`. Leave the
sidecar containers up, so WhatsApp stays connected. For cache-loss testing, first
stop the worker and back up `.local/agent.sqlite3`; preserve the vault key, wallet
files, Arkiv send journal and WhatsApp progress database. Task recovery then reads
Arkiv before the worker takes new work.

The Docker volumes hold the linked-device keys and Redis events. Preserve them
when stopping the services. Do not use `down --volumes` for a normal restart.

The source context is `rc-harness copy/ira/wa`; this local setup requires that
existing checkout. Building it does not copy production credentials or deploy to
production. All published ports bind to loopback: sidecar 58083, Redis 56389.

## Read receipts, typing and reactions

Accepted user texts are marked read through `/read/{line}`. Typing starts when
Ira uses tools, and briefly before each written bubble is sent. It ends on
completion, interruption or error. A completed reaction-only reply sends no
text bubble and does not show typing. Slow inference alone does not produce a
progress bubble; progress notices require actual tool work.

Contextual reactions use a separate model request alongside the normal reply.
It sees at most six recent messages (200 characters each) and the newest text
(1,000 characters). It has a three-second deadline and accepts a model-provided
confidence rating of at least 0.5. That rating is not a calibrated probability.
The `reaction` inference route uses the existing OpenAI key with a small output
budget and reasoning disabled, leaving normal reply settings unchanged. Custom
configurations can select a provider through that same route; without it the
existing default route is used. There are no keyword rules or extra inference retries.

The model's final reply contains `emoji_reaction`, then `response_messages`.
An empty array sends no words. Only completion of the main turn marks the
input answered; an early reaction does not complete a request or a background
task. Only the newest eligible user text in each incoming Redis batch receives
a reaction. Votes, reaction events and card taps do not qualify.

Private chat events retain the original WhatsApp message ID. A reaction updates
that user event's `emoji_reaction` and appends `chat_reaction` with
`message_event_id` for `/events` consumers. WhatsApp delivery uses `/react/{line}`
to attach the emoji to the original bubble. Existing reactions are preserved,
and uncertain reaction sends follow the existing no-repeat delivery behavior.

The compose configuration sets `WA_LINE_READ_DELAY_MS=0` so a successful read
request awaits the read operation, instead of scheduling it after the response.
The Python agent adds a short, varied reaction delay based on text length;
emotional reactions appear faster. Apply the compose environment change when
recreating the local sidecar, then restart the Python agent to load this code.
Handset appearance and blue ticks still depend on WhatsApp delivery and the
recipient's read-receipt settings.
