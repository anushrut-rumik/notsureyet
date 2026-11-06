# Actual CRE arithmetic check

This separate simulation-only workflow runs the same `lending/run.ts` evaluator
with labelled immutable arithmetic fixtures. It has no RPC/HTTP client, report,
wallet, signer or transaction capability. It does not replace the Part 2 deployed
contract workflow and does not establish testnet funding, contract reads or yield.

Run `npm run lending:cre:arithmetic -- new-evidence-directory` from the repository
root after CRE login. The script writes fresh immutable move/hold inputs, invokes
the actual CRE CLI from `cre-lending`, compares against local results at the exact
returned CRE timestamp, and saves full public evidence. Both use the original
Part 1 policy thresholds and fictional values; only the fixture time is rebased.

The native command for either generated input is:

```sh
cre workflow simulate arithmetic-check --target local-simulation --non-interactive --trigger-index 0 --http-payload /absolute/path/to/input.json
```

Never deploy this workflow or add broadcast. Its `local-simulation` config is
closed and does not admit a receiver or real observations.
