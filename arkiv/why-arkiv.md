# Why Arkiv

## What would users lose if this stayed in SQLite/Postgres?

An independently queryable copy of task state owned by their selected wallet.
Lists and progress query Arkiv. A separate SDK client reads public state without
our HTTP server. With authorized encryption-key access, fresh Ira reconstructs
private task progress and already-submitted payment identifiers after losing its
execution database.

Conventional databases also implement durability/recovery. Arkiv contributes the
external query surface and wallet-owned records instead of relying only on our
database/API. Public readers inspect metadata; key holders recover private contents.
Ownership does not eliminate custody, fees or availability risks.

Default task lists, creation, progress and recovery depend on Arkiv. Removing it
stops these paths. Explicit SQLite development mode is separate and disables
payments. [Query](../README.md), [recovery evidence](demo.md).

## What stays off Arkiv, and why?

- General chat/preferences: avoid publishing every conversation's metadata/cost.
- Signing/encryption keys and credentials: reading an entity never grants spending.
- Generated workspace files: a checkpoint reference does not restore file bytes
  absent from its snapshot.
- WhatsApp routing/deduplication/delivery admission: an Arkiv receipt cannot prove
  handset delivery.
- Local payment admission journals: protect checkpoint gaps. Newer verified Arkiv
  state cannot be overwritten by an older local draft. Clean-instance tests omit
  those journals entirely.
