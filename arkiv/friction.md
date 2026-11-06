# Friction and current limits

- The application is Python; the official 1.0.1 SDK is used through a small Node
  bridge. Both `uv sync` and `npm ci` are needed.
- Plugin installation supplies guidance, not SDK installation, wallet custody or gas.
  The installed Mumbai endpoint reported server version 2.1.0 during this session.
- Rabdi access keys are URL path segments. The app keeps them server-side and
  suppresses raw transport diagnostics. Keyless public reads worked in this session,
  but are heavily limited and unsuitable as a reliability claim.
- Every immutable checkpoint costs a transaction. The first version saves several
  barriers per model/tool turn. It favors inspectable recovery over low latency/cost.
- Bytes fields are public. Task content requires independent encryption-key custody;
  immutable records still expose metadata and cannot retract already copied content.
- Current queries are latest-state observations with bounded keyset traversal, not
  pinned historical snapshots. Stop the sole writer for a stable recovery scan.
- 30-day retention, weekly active-task refresh, 85 KB plaintext and 5,000 revision
  bounds are application choices. A longer outage, oversize task or network reset
  needs operator attention. There is no automatic resubmission workaround.
- No live task write/payment/WhatsApp delivery has been claimed. Current tests use
  simulated provider responses around real SDK/signing paths; funded live testing remains.
