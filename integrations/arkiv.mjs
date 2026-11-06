// SDK 1.0.1 on Rabdi. Only main() reads credentials, from stdin, never arguments.
import { createPublicClient, createWalletClient, ExpirationTime } from '@arkiv-network/sdk';
import { defineSchema, str, bytes, u64, key } from '@arkiv-network/sdk/schema';
import { eq, and, gt, asc } from '@arkiv-network/sdk/query';
import { rabdi } from '@arkiv-network/sdk/chains';
import { http, custom, MethodNotFoundRpcError, keccak256, toHex } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { readFileSync, writeFileSync, renameSync, openSync, fsyncSync, closeSync, existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export const Tasks = defineSchema('ira_task_checkpoint_v1', {
  instance: str().indexed(), task: str().indexed(), revision: u64().indexed(),
  operation: str().indexed(), encrypted: bytes(),
});
export function saveJournal(path, value) {
  const tmp = path + '.tmp';
  writeFileSync(tmp, JSON.stringify(value), { mode: 0o600 });
  const fd = openSync(tmp, 'r'); fsyncSync(fd); closeSync(fd);
  renameSync(tmp, path);
  const dir = openSync(new URL('.', pathToFileURL(path)), 'r'); fsyncSync(dir); closeSync(dir);
}
export function gasCap(estimate, blockLimit) {
  const gas = 2n * estimate > 1000000n ? 2n * estimate : 1000000n;
  if (estimate <= 0n || gas > blockLimit / 2n || gas > 2n ** 24n) throw Error('Unsigned gas preparation failed');
  return gas;
}
export function trusted(row, writer, instance) {
  return row.$creator.toLowerCase() === writer.toLowerCase()
    && row.$owner.toLowerCase() === writer.toLowerCase()
    && row.$creationFlags?.readonly === true && row.instance.value === instance;
}
export async function readAll(reader, writer, instance, task) {
  const rows = []; let last;
  for (let i = 0; i < 50; i++) {
    const filters = [eq('instance', str(instance))];
    if (task) filters.push(eq('task', str(task)));
    if (last) filters.push(gt('$key', key(last)));
    const page = await reader.select('*').from(Tasks, { onMismatch: 'throw' })
      .createdBy(writer).ownedBy(writer).where(and(...filters))
      .orderBy(asc('$key')).limit(100).fetch();
    for (const row of page.entities) {
      if (!trusted(row, writer, instance)) throw Error('Untrusted checkpoint');
      rows.push({ entity_key: row.$key, task: row.task.value, revision: row.revision.value.toString(),
        encrypted: Buffer.from(row.encrypted.value.slice(2), 'hex').toString(),
        expires_at_block: row.$expiresAt.toString(), operation: row.operation.value });
    }
    if (page.entities.length < 100) return rows;
    const next = page.entities.at(-1).$key;
    if (last && next <= last) throw Error('Query did not advance');
    last = next;
  }
  throw Error('Checkpoint page limit reached; no incomplete recovery allowed');
}
async function estimate(account, chain, rpc, values, options, txParams) {
  let captured; const stop = Error('estimate captured');
  const reads = new Set(['eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber',
    'eth_getTransactionCount', 'eth_gasPrice', 'eth_maxPriorityFeePerGas', 'eth_estimateGas']);
  const transport = custom({ async request(args) {
    if (args.method === 'eth_fillTransaction') throw new MethodNotFoundRpcError(Error('Use estimate'));
    if (!reads.has(args.method)) throw Error('Mutation blocked during unsigned estimation');
    const result = await rpc.request(args.method === 'eth_estimateGas'
      ? { method: args.method, params: [args.params[0], 'latest'] } : args);
    if (args.method === 'eth_estimateGas') { captured = BigInt(result); throw stop; }
    return result;
  } }, { retryCount: 0 });
  const unsigned = { ...account, signTransaction: async () => { throw Error('Signing blocked'); } };
  try { await createWalletClient({ account: unsigned, chain, transport })
    .createEntity(Tasks, values, options, txParams); }
  catch (e) {
    for (let cause = e, i = 0; cause && i < 24; i++, cause = cause.cause)
      if (cause === stop && captured) return captured;
  }
  throw Error('Unsigned preparation failed');
}
export async function execute(input, rpcOverride) {
  const chain = rabdi({ accessKey: input.access_key ?? '' });
  const rpc = rpcOverride ?? http(undefined, { retryCount: 0, timeout: 15000,
    fetchOptions: { redirect: 'error', cache: 'no-store' } })({ chain });
  const reader = createPublicClient({ chain, transport: custom(rpc, { retryCount: 0 }), cacheTime: 0 });
  if (await reader.getChainId() !== 7794095) throw Error('Wrong Arkiv network');
  if (input.command === 'read') return { rows: await readAll(reader, input.writer, input.instance, input.task) };
  if (input.command === 'funding') return { balance_wei: (await reader.getBalance({address: input.writer})).toString(),
    chain_id: 7794095, asset: 'tGLM', network: 'Arkiv Rabdi' };
  // A single local process holds the signer lock, including reconciliation.
  let journal = existsSync(input.journal) ? JSON.parse(readFileSync(input.journal, 'utf8')) : null;
  if (journal?.instance && journal.instance !== input.instance && journal.state !== 'verified')
    throw Error('This wallet already has a checkpoint pending in another chat');
  if (input.command === 'attach_external') {
    if (!/^0x[0-9a-fA-F]{64}$/.test(input.tx_hash ?? '') || !journal
      || !['external_claimed', 'external_submitted', 'verified'].includes(journal.state)) throw Error('No admitted wallet request');
    if (journal.hash && journal.hash.toLowerCase() !== input.tx_hash.toLowerCase()) throw Error('Conflicting transaction hash');
    const tx = await reader.getTransaction({hash: input.tx_hash});
    const expected = journal.transaction;
    if (!expected || tx.from.toLowerCase() !== input.writer.toLowerCase()
      || tx.to?.toLowerCase() !== expected.to.toLowerCase() || tx.input !== expected.data
      || BigInt(tx.nonce) !== BigInt(expected.nonce) || tx.value !== 0n
      || tx.gas !== BigInt(expected.gas) || tx.gasPrice !== BigInt(expected.gasPrice)
      || tx.chainId !== 7794095) throw Error('Wallet transaction differs from the prepared checkpoint');
    journal = {...journal, hash: input.tx_hash, state: 'external_submitted'};
    saveJournal(input.journal, journal);
  }
  if (journal?.state === 'external_prepared' && input.command === 'claim_external') {
    const tx = journal.transaction;
    const nonce = await reader.getTransactionCount({address: input.writer, blockTag: 'latest'});
    const pending = await reader.getTransactionCount({address: input.writer, blockTag: 'pending'});
    const gasPrice = await reader.getGasPrice();
    const gas = BigInt(tx.gas);
    const estimated = BigInt(await rpc.request({method: 'eth_estimateGas', params: [tx, 'latest']}));
    const block = await reader.getBlock({blockTag: 'latest'});
    if (nonce !== Number(BigInt(tx.nonce)) || nonce !== pending || gasPrice > BigInt(tx.gasPrice)
      || gas < gasCap(estimated, block.gasLimit) || gas > block.gasLimit / 2n
      || gas * BigInt(tx.gasPrice) > BigInt(input.max_fee_wei)
      || await reader.getBalance({address: input.writer}) < gas * BigInt(tx.gasPrice)) throw Error('Checkpoint preflight changed');
    saveJournal(input.journal, {...journal, state: 'external_claimed'});
    return {transaction: tx};
  }
  if (input.command === 'claim_external') throw Error('This wallet request already started; reconcile it without resending');
  if (journal?.state?.startsWith('external_') && !journal.hash) {
    if (input.command === 'reconcile') return {pending: true};
    if (input.command === 'prepare_external' && journal.task === input.task) return {pending: true};
    throw Error('A wallet checkpoint is already pending; do not replace it');
  }
  if (journal && journal.state !== 'verified') {
    if (!journal.hash) throw Error('Uncertain Arkiv admission; manual reconciliation required');
    const receipt = await reader.getTransactionReceipt({ hash: journal.hash });
    if (receipt.status !== 'success') throw Error('Arkiv receipt reverted; no automatic retry');
    const found = (await readAll(reader, input.writer, input.instance, journal.task))
      .find(r => r.operation === journal.operation && r.encrypted === journal.encrypted);
    if (!found) throw Error('Receipt exists but checkpoint readback failed; no resend');
    journal = { ...journal, state: 'verified', result: found }; saveJournal(input.journal, journal);
  }
  if (input.command === 'reconcile') return { reconciled: true };
  if (input.command === 'attach_external') return journal.result;
  const account = input.command === 'prepare_external'
    ? {address: input.writer, type: 'json-rpc'} : privateKeyToAccount(input.private_key);
  if (account.address.toLowerCase() !== input.writer.toLowerCase()) throw Error('Writer mismatch');
  const code = await reader.getCode({address: input.writer});
  if (code && code !== '0x')
    throw Error('Contract wallets are not supported by the Rabdi native write path');
  const operation = keccak256(toHex(JSON.stringify([input.instance, input.task, input.revision, input.encrypted])));
  if (journal?.operation === operation) return journal.result;
  const values = { instance: input.instance, task: input.task, revision: u64(BigInt(input.revision)),
    operation, encrypted: Buffer.from(input.encrypted) };
  const options = { expiration: ExpirationTime.fromDays(30), flags: { readonly: true },
    salt: BigInt(operation) & ((1n << 128n) - 1n) };
  const nonce = await reader.getTransactionCount({ address: account.address, blockTag: 'latest' });
  const pending = await reader.getTransactionCount({ address: account.address, blockTag: 'pending' });
  if (nonce !== pending) throw Error('Writer has pending transactions; reconcile first');
  const gasPrice = BigInt(await rpc.request({ method: 'eth_gasPrice' }));
  const observed = await estimate(account, chain, rpc, values, options, { nonce, gasPrice });
  const latest = await reader.getBlock({ blockTag: 'latest' });
  const gas = gasCap(observed, latest.gasLimit);
  const cost = gas * gasPrice;
  if (cost > BigInt(input.max_fee_wei) || cost > await reader.getBalance({ address: account.address }))
    throw Error('Arkiv fee exceeds configured budget or writer balance');
  journal = { state: 'prepared', operation, task: input.task, revision: input.revision,
    instance: input.instance, writer: input.writer, encrypted: input.encrypted };
  if (input.command === 'prepare_external') {
    let transaction;
    const stop = Error('Captured unsigned wallet transaction');
    const transport = custom({async request(args) {
      if (args.method === 'eth_sendTransaction') {transaction = args.params[0]; throw stop;}
      if (/^eth_send|^wallet_/.test(args.method)) throw Error('Signing blocked');
      return rpc.request(args);
    }}, {retryCount: 0});
    try {await createWalletClient({account, chain, transport, cacheTime: 0})
      .createEntity(Tasks, values, options, {gas, nonce, gasPrice});} catch { /* Captured below; never forward sends. */ }
    if (!transaction) throw Error('Could not prepare wallet checkpoint');
    transaction = {...transaction, value: '0x0', chainId: '0x76edaf'};
    saveJournal(input.journal, {...journal, state: 'external_prepared', transaction});
    return {pending: true};
  }
  saveJournal(input.journal, journal);
  const transport = custom({ async request(args) {
    if (args.method === 'eth_sendRawTransaction') {
      const raw = args.params[0];
      journal = { ...journal, state: 'submitting', hash: keccak256(raw), raw };
      saveJournal(input.journal, journal); // durable before provider admission
    } else if (/^eth_send|^wallet_/.test(args.method)) throw Error('Unexpected signing path');
    return rpc.request(args);
  } }, { retryCount: 0 });
  const wallet = createWalletClient({ account, chain, transport, cacheTime: 0 });
  const result = await wallet.createEntity(Tasks, values, options, { gas, nonce, gasPrice });
  journal = { ...journal, state: 'receipt_confirmed', hash: result.txHash };
  saveJournal(input.journal, journal);
  const receipt = await reader.getTransactionReceipt({ hash: result.txHash });
  if (receipt.status !== 'success') throw Error('Arkiv receipt failed');
  const row = (await readAll(reader, input.writer, input.instance, input.task))
    .find(r => r.entity_key === result.entityKey && r.operation === operation && r.encrypted === input.encrypted);
  if (!row) throw Error('Checkpoint readback failed; no resend');
  journal = { ...journal, state: 'verified', result: row }; saveJournal(input.journal, journal);
  return row;
}
async function main() {
  try {
    let input = ''; for await (const chunk of process.stdin) input += chunk;
    const parsed = JSON.parse(input);
    const result = await execute(parsed);
    process.stdout.write(JSON.stringify({ ok: true, result }));
  } catch (error) {
    // Never forward RPC URLs, credentials, raw transactions or encrypted payloads in diagnostics.
    let status;
    for(let cause=error, i=0;cause && i<24;i++,cause=cause.cause)
      if(Number.isInteger(cause.status)) status=cause.status;
    process.stdout.write(JSON.stringify({ ok: false,
      error: status===429 ? 'Arkiv public RPC is rate limited. Configure AGENT_ARKIV_ACCESS_KEY and wait before retrying.'
        : 'Arkiv request failed. Check access, funding and the per-wallet send journal; reconcile before retrying.',
      ...(status ? {http_status: status} : {}) }));
    process.exitCode = 1;
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
