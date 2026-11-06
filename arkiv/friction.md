# Arkiv feedback

## What got in our way, and what would help?

- Version selection is application work. Filtering history for running tasks can
  hide a newer completion. We added an atomic current index plus readonly history
  and longer terminal retention. A documented current-record/terminal-marker recipe
  would help existing agents integrate safely.
- Native patching has no conditional expected-revision update. Local wallet locks
  support one process, not independent machines. Compare-and-set would simplify
  coordinated agent workers; we do not claim a distributed task lock.
- Receipt success, SDK event decoding and field readback fail independently.
  Durable signed bytes/hash before admission protect ambiguous writes. A lost
  response must not authorize another payment/checkpoint send.
- Python invokes SDK 1.0.1 through Node, requiring both dependency sets and adding
  latency. Tool-heavy tasks need many fee-bearing checkpoint barriers.
- Unindexed bytes are public. Encryption-key recovery and public transaction
  metadata need explicit design. We suppress transport diagnostics to avoid
  exposing keyed RPC URLs.
- Multipage queries are current observations, not atomic backups. Co-ordinate
  writers for stable scans. Expiry has no event; refresh/outage/reset behavior is
  application responsibility.

## Expected behavior, failure cases and reproduction

Run npm test and uv run pytest -q tests/test_arkiv_current.py tests/test_web3.py.

These deterministic application regressions replay running state after completion,
remove/duplicate active records, tamper metadata independently of ciphertext,
lose a send response, and restore stale local payment journals after delivery.
Expected: terminal tasks remain terminal; inconsistent active state stops work;
known hashes survive; zero extra payments on recovery. They pass with this change.
These are not claims of newly measured provider defects. [Live evidence](demo.md)
is separate.

Bounds: 85 KB plaintext per checkpoint; 5,000 rows per schema scan. Exceeding either
fails rather than returning truncated recovery. Anonymous RPC is heavily limited.
Access keys remain server-side. A chain-ID probe is not write/delivery evidence.
