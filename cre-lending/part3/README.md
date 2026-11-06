# Part 3 testnet execution

This workflow uses the shared decision engine and validates both immutable
observations and current finalized balances, policy binding, pool rates, router
fees and unresolved operations before producing a report. The consumer requires
an owner EIP-712 signature over the exact input and action.

The testnet mock forwarder does not authenticate workflow metadata. This is a
user-authorized test simulation, not production DON authentication. The separate
receiver-free target performs no reads, reports or writes. No DON deployment is
included. See [the complete guide](../../docs/lending-part3.md).

From the project root `cre-lending`:

```sh
cre workflow simulate part3 --target local-simulation --non-interactive --trigger-index 0 --http-payload '{}'
cre workflow simulate part3 --target staging-settings --config config.report-preview.json --non-interactive --trigger-index 0 --http-payload '{}'
```

The second command generates only a report preview. It never submits it.

For an admitted, journaled, user-signed execution payload, run the printed
non-broadcast `staging-settings` command first. The journal claim must be durable
before the user adds `--broadcast`. Attach the returned source hash and run the
monitor. Simulator success is not destination deposit proof.

This skill never signs or sends. You must sign and broadcast from your own wallet.
