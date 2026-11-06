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
