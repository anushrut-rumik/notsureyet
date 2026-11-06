# MetaMask setup for Part 2

This is a temporary local signing review for the already built test contracts.
It inherits the existing wallet page's plain HTML/CSS layout. It does not add a
product dashboard, connect WhatsApp, send CCIP messages or execute rebalances.

The Node server binds only to `127.0.0.1:8799`, checks its Host and Origin, and
requires a per-session local review URL. It has no wallet client, private key,
keystore access or RPC send method. Only an explicit user button in the browser
can ask the MetaMask extension to sign the public plan or a displayed transaction.
Each transaction requires a separate wallet confirmation. All source code and
public calldata are local and reviewable.

From the repository root:

```sh
npm run lending:build
npm run lending:setup:build
node --test tests/lending-setup.test.mjs
npm run lending:setup -- 0x66051Eef2e7DD1b410C6E68EeDF8ABBddB783Ee1
```

Open the printed URL in the desktop browser containing the funded MetaMask
account. The proposed plan uses 0.8 BnM principal, 0.2 BnM reserve per strategy
wallet, and 1 test LINK per strategy wallet. Source funding transfers 1 BnM, then
deposits exactly 0.8. Destination funding transfers 0.2 BnM and deposits none.
Constructor and transfer amounts, token addresses, both chain IDs, nonce sequence,
all deployment bytes and the fixed deadline are included in the owner's typed
setup-plan signature. Future calls still need ordinary transaction signatures.
The fictional valuations and extreme 10,000% destination mock rate are displayed
before approval. The rate never produces interest. Benefit/fee limits stay at
1 SIM_USD and 3 SIM_USD as displayed (1,000,000 and 3,000,000 microunits).

The flow has 11 initial setup transactions and a final owner-only mock rate
change. Before a request reaches MetaMask, the server durably claims the action
and verifies network, owner nonce, previous receipts and deployment prerequisites.
It rejects duplicate/uncertain submissions. The browser immediately saves any
returned hash locally; the server verifies sender, target, nonce, calldata and
value before accepting it. A real successful canonical creation receipt with
code is required. A zero hash, different transaction, revert or stale nonce
cannot advance the setup. A confirmed receipt is not yet a finality claim.

MetaMask can relay a transaction from a smart account. In that case the outer
transaction sender is the relayer. The server verifies the canonical receipt
and `trace_transaction` evidence for the exact successful call from the owner,
including the target, calldata and zero ETH value. It rejects reverted enclosing
calls and extra owner calls. Relays must leave the owner's native nonce unchanged;
later transaction requests account for those verified relays without changing
the signed plan or any contract creation address. RPCs without tracing cannot
verify a relay; keep its hash and do not resend. Exported manifests include exact
funding calls so the final deployed-data checker can verify relayed funding too.

If a browser closes during signing, recover the existing transaction hash from
MetaMask Activity. Do not resend an uncertain action. Definite wallet rejection
can release an unsigned claim only while the expected nonce remains unused.
Use one setup-server process. `server.lock/pid` prevents two local processes from
claiming the same state; remove a stale lock only after confirming its process is
stopped. The public journal remains at `.local/lending-metamask/state.json`.
The local review token is not a signing key and is stored beside that journal to
keep the review URL stable across a restart.

Once the first 11 receipts are recorded, the server exports `manifest.json`.
Continue verification without signing or sending:

```sh
node scripts/lending-finish-part2.mjs move .local/lending-metamask
```

That command waits for finalized receipts, verifies the exact typed setup
approval, creates a fresh immutable run, reads actual rates/balances/fee quotes,
runs the actual CRE simulator and compares at its returned clock. The source gas
budget is an explicitly estimated 600,000 units, using the higher of the current
RPC price and observed setup transaction effective prices. Destination pool
approval/deposit gas is included in the router's 300,000-gas message quote; no
second destination approval charge is added. Exit costs are explicitly controlled
equal-zero scenario assumptions, not live withdrawal quotes. All original fee
amounts, assumptions and fictional prices are saved separately. Policy deadline,
minimum benefit, fee cap and freshness bounds are preserved.

The user-run hold-rate transaction stays locked until the exact deployed-data
move evidence shows unchanged balances, successful exit code and local parity for
this same owner and policy version. After that owner-only rate transaction is
signed and finalized, run:

```sh
node scripts/lending-finish-part2.mjs hold .local/lending-metamask
```

The final two evidence files must show `move` and `hold`, respectively, plus
`localMatchesCRE: true` and `balancesUnchanged: true`. Neither command adds the CRE
broadcast flag or invokes a signing tool. Do not describe setup or Part 2 as
complete until those deployed-contract checks pass.

This skill never signs or sends. You must sign and broadcast from your own wallet.
