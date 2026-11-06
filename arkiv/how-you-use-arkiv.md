# How this application uses Arkiv

Implementation: `agent/arkiv.py` and `integrations/arkiv.mjs`.

1. Select the chat's active wallet, create a task and publish an encrypted immutable checkpoint.
   Agent wallets sign automatically; connected MetaMask wallets approve each checkpoint.
   Pending records remain local and block execution until verified on Arkiv.
2. Query the schema, pinned creator/owner and instance for chat task listings.
3. Persist tool/effect barriers and payment hashes before external admission.
4. Check receipts and publish verified task progress.
5. At startup, query/decrypt checkpoints before claiming any task in SQLite.
6. Recover submitted payments by their saved hashes without another send.

Unsigned estimation precedes every new Arkiv signature. Fresh latest nonce, pending
nonce check and gas price are obtained under the local signer lock. Gas is at least
1,000,000 and twice the exact estimate, and must fit half the block. A configured
per-write fee budget and writer balance bound spending. Signed bytes/hash are
journaled and fsynced before provider admission. Uncertain writes are reconciled,
never automatically resent. Receipt confirmation and readback are distinct phases.

Run `/arkiv` in chat for its active wallet address, live tGLM balance and public scope.
`agent web3-init` initializes encryption without generating a separate writer wallet.
Run `node --env-file=.env integrations/read-tasks.mjs WRITER INSTANCE` for the
reproducible independent query. Actual deployment evidence and limitations belong
in README; installation or fixture success alone is not a live write.
