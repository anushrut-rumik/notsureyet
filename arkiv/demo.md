# Arkiv demonstration and evidence

Verified on 2026-11-06. These observations establish storage/recovery, not sponsor
eligibility, public cross-chain payment delivery or handset experience.

## 1. Live Rabdi current state and checkpoints

Writer: `0x29a73a629FFd5323dDcA4A474210d38d2722D41d`.
Instance: `8ad85568f8a12be483ef170103a4bdf71d4a187113d74524b183d37bf7fb668b`.
Task: `e5d554be8a4144d1a2a1696677c530b0`.
Current entity: `0xcff2d21d69d3e5463eb2469345f031a82030e17008848b8912f3f65e1e41ba67`.

- [First batch](https://rabdi.explorer.arkiv.network/tx/0xa329e795b717259859ed93c6625e1e215ab37e4158d6b0709c910df0c49d0764): current record + readonly checkpoint.
- [Second batch](https://rabdi.explorer.arkiv.network/tx/0x1cbcd7ec615b2c6c77badb17063c3c931fa9f311d756bfd2e454c1cceb3fa3b4): patch same current record + new checkpoint + lifetime extension.

Both receipts succeeded. Independent readback checks creator/owner, readonly flags,
revision, operation and equality of current/checkpoint fields and ciphertext.
[Machine-readable live storage evidence](evidence/live-storage.json).
The demonstration uses a separate scope so it does not move or overwrite the
original chat's tasks. It archives an already-existing result, not a new payment.

Run the exact independent query in README. Expected public state is completed /
simulation / chainlink_local; source/destination transaction hashes are empty.

## 2. A fresh Ira runtime with authorized key access

[Recovery evidence](evidence/live-recovery.json) came from an empty directory and
SQLite database. Only the encryption key and a trusted scope descriptor were
restored. No wallet signing files, payment journals or saved simulation files were
copied. Runtime recovery and user-visible task listing read the live Rabdi records.
The final task was completed and the saved simulation message ID was unchanged.
Inference and mutation admission were disabled: zero payments and zero reruns.

On an authorized local machine, with the demo's private source directory retained:

```sh
uv run python scripts/recover-arkiv-task.py \
  --source-root .local/arkiv-v2-demo \
  --destination .local/your-new-clean-instance \
  --task-id e5d554be8a4144d1a2a1696677c530b0
```

The destination must not already exist. It writes a local recovery-report.json.
Keys are intentionally absent from this public repository. Public judges can run
the SDK metadata query without keys; private-content recovery requires permission
to the original encryption key and scope descriptor. Never upload keys to prove it.

## 3. Live pending CCIP intent recovered without signing

Later in the session, a real testnet CCIP request was created through the existing
approval flow. A second fresh Ira Runtime recovered that exact task from Rabdi:
`ebee7d898f5e4e6f8b2d91acb3d0a5b8`, payment reference
`9b4eb2dd6993400b9942cfb5e1f3e8bc`. It requests 0.0001 CCIP-BnM to Base Sepolia.

[Live pending-intent recovery report](evidence/live-pending-intent-recovery.json):
empty database, only encryption key/scope restored, no wallet keys/payment journals,
no inference and zero payment submissions. At observation, progress was
`awaiting_signature` for the token allowance, with no source hash or CCIP message ID.
The public order_id at this stage is the intent ID, not a delivered-message receipt.

```sh
node --env-file=.env integrations/read-tasks.mjs \
  0x29a73a629FFd5323dDcA4A474210d38d2722D41d \
  a773f7e82d3ad7f479ca53b859396fcf60f3f3e76724a8de8762e63088de63e0 \
  ebee7d898f5e4e6f8b2d91acb3d0a5b8
```

This reads current live progress, which may advance after the recorded observation.
Completion cannot be claimed until the user's existing allowance/send approvals
and actual finalized delivery occur. The recovery demonstration does not approve
those actions or create a replacement transfer.

## 4. Submitted CCIP intent recovered and tracked to completion in controlled tests

```sh
uv run pytest -q \
  tests/test_arkiv_current.py::test_clean_instance_recovers_submitted_ccip_and_finishes_without_another_payment
```

This controlled-RPC regression uses the real payment approval/signing code before
restart, then creates a clean store with only authorized encryption-key access.
No payment wallet key or payment journal is copied. The test blocks every payment
send RPC after restart, checks the existing source hash/message ID, verifies
finalized destination execution and token movement, invokes ccip_delivery_receipt,
then completes through finish_task with that evidence event. A second recovery
cannot claim the terminal task. One source submission before restart; zero after.
[Controlled evidence](evidence/controlled-ccip-recovery.json).

The endpoint responses and identifiers in this report are fixtures, not public
chain transactions. The live user's latest saved result was a Chainlink Local
simulation of 0.0001 CCIP-BnM. Its 0.001 ETH value was the simulated fee. No submitted
public cross-chain payment was available in the inspected records, so public
Sepolia-to-Base payment recovery remains unverified. No replacement payment was
created to manufacture that evidence.

## 5. Terminal-state and authorization regressions

```sh
npm test
uv run pytest -q
```

Cases include old/reopened heads after completion, missing/duplicate/mismatched
active records, tampered public metadata, stale local payment journals, lost send
responses, wallet changes, user isolation, and approval/checkpoint failures before
external execution. Pending storage signatures never mean data is already stored.

Limits: one coordinated writer per wallet, 85 KB checkpoint plaintext, bounded
current-state scans, explicit expiry, local key custody and testnet reset risk.
The loopback HTTP API has no production identity verification.
