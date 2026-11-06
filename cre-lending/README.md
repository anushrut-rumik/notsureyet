# Part 2: testnet observations and read-only CRE evaluation

This project uses the shared `lending/run.ts` evaluator. It does not create a CRE
report, call `writeReport`, send HTTP requests, or submit RPC transactions. Even a
`move` result is only a recommendation. Do not deploy this evaluation workflow or
enable CRE broadcast for rebalancing in Part 2.

The project was written after `cre init --non-interactive --project-name
cre-lending --deployment-registry private --workflow-name workflow --template
hello-world-ts` initially failed because CLI authentication was missing. Login is now verified,
and the separate arithmetic check has passed in the actual simulator. No generic sample
handler, mainnet RPC, sample secret or production target remains.

From the repository root:

```sh
npm ci
npm run ccip:deps
npm run lending:part2:test
npm run lending:testnet -- preflight
cre login
```

Complete browser authentication yourself. No key is needed in the agent/chat.
`npm` commands use the pinned Bun 1.3.2 instead of the host's older Bun 1.2.5.
Dependencies are locked at repository level; run `npm ci` there, not in `workflow`.
`npm run lending:cre:build` creates ignored `.local/lending-cre/main.wasm`.

For an immutable payload prepared from **your own** funded test deployments, run
from this directory (the root containing `project.yaml`):

```sh
cre workflow simulate workflow --target staging-settings --non-interactive --trigger-index 0 --http-payload ../.local/lending-review/move-input.json
```

The HTTP trigger needs its per-run payload. The shorter template command
`cre workflow simulate workflow --target staging-settings --non-interactive
--trigger-index 0` was actually attempted and stopped at authentication before
requesting trigger data. A successful compilation or SDK harness test alone does not
establish actual CRE simulator execution. The separate `arithmetic-check` now
has successful native move/hold evidence; deployed-contract evidence is pending.

Prefer the evidence wrapper from the repository root:

```sh
npm run lending:testnet -- simulate .local/lending-review/move-input.json .local/lending-review/move-evidence.json
```

It invokes the above command from the correct directory, saves the actual command
and simulator output, compares the returned decision with local evaluation of the
same immutable run at CRE's returned clock, and checks native/token/principal
balances before and after. It fails on any discrepancy or missing result. Output
files use exclusive creation and cannot silently overwrite a previous run.
Set `CRE_CLI_PATH` to a CLI executable if it is not on PATH or at `~/.cre/bin/cre`.
No workflow lifecycle/deployment operation is included. The explicit `private`
registry selector prevents fallback to an onchain workflow registry; it does not
provide a network safety boundary. Chain IDs, selectors and infrastructure are
checked independently.

See [the complete setup and funding guide](../docs/lending-part2.md).


For the separate arithmetic-only native check (no owned contracts needed):

```sh
npm run lending:cre:arithmetic -- .local/new-native-arithmetic-evidence
```

Run that npm command from the repository root. It runs the actual CRE CLI twice,
labels every input fictional and compares each decision locally. See
[wallet creation instructions](../docs/lending-part2.md#create-the-dedicated-test-wallet)
for the next onchain setup step.
