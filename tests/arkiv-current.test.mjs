import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { privateKeyToAccount } from 'viem/accounts';
import { execute } from '../integrations/arkiv.mjs';
import { publicFields } from '../integrations/task-state.mjs';

const privateKey = '0x' + '11'.repeat(32);
const writer = privateKeyToAccount(privateKey).address;
const instance = 'test';
const headKey = '0x' + 'aa'.repeat(32), checkpointKey = '0x' + 'bb'.repeat(32);
const state = { ...Object.fromEntries(publicFields.map(name => [name, ''])),
  status: 'waiting', capability: 'payment', protocol: 'ccip', progress: 'in_transit',
  source_network: 'ethereum_sepolia', destination_network: 'base_sepolia',
  reference: 'payment-1', source_tx: '0x' + '12'.repeat(32), order_id: '0x' + '34'.repeat(32) };
function entity(schema, revision = 2, status = 'waiting', operation = 'op-2', key = checkpointKey) {
  const immutable = schema === 'ira_task_checkpoint_v2';
  return { key, owner: writer, creator: writer, expiresAt: '0xffffff',
    creationFlags: { readonly: immutable, permissionlessExtension: false, raw: immutable ? 1 : 0 },
    attributes: [{name: 'arkiv.schema', type: 'str', value: schema},
      {name: 'instance', type: 'str', value: instance}, {name: 'task', type: 'str', value: 'task-1'},
      {name: 'revision', type: 'u64', value: '0x' + revision.toString(16)},
      {name: 'operation', type: 'str', value: operation}, {name: 'encrypted', type: 'bytes', value: '0x616263'},
      ...Object.entries({...state, status}).map(([name, value]) => ({name, type: 'str', value}))] };
}
function network(heads, history, extra) {
  return {request: async ({method, params}) => {
    if (method === 'eth_chainId') return '0x76edaf';
    if (method === 'arkiv_query') {
      const query = JSON.stringify(params);
      const data = query.includes('ira_task_current_v2') ? heads
        : query.includes('ira_task_checkpoint_v2') ? history : [];
      return {blockNumber: '0x1', data};
    }
    if (method === 'arkiv_getEntity') return history.find(row => row.key === params[0]);
    if (extra) return extra(method, params);
    throw Error(method);
  }};
}
test('current state exposes protocol references and authenticates the matching immutable checkpoint', async () => {
  const result = await execute({command: 'read', writer, instance, access_key: ''},
    network([entity('ira_task_current_v2', 2, 'waiting', 'op-2', headKey)], [entity('ira_task_checkpoint_v2')]));
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].current_key, headKey);
  assert.equal(result.rows[0].public_state.order_id, state.order_id);
  assert.equal(result.rows[0].public_state.progress, 'in_transit');
});
test('terminal checkpoint defeats replayed running head and a later reopened revision', async () => {
  const history = [entity('ira_task_checkpoint_v2', 2, 'completed'),
    entity('ira_task_checkpoint_v2', 3, 'queued', 'op-3', '0x' + 'cc'.repeat(32))];
  const result = await execute({command: 'read', writer, instance, access_key: ''},
    network([entity('ira_task_current_v2', 1, 'running', 'op-1', headKey)], history));
  assert.equal(result.rows[0].public_state.status, 'completed');
  const withoutHead = await execute({command: 'read', writer, instance, access_key: ''}, network([], history));
  assert.equal(withoutHead.rows[0].public_state.status, 'completed');
});
test('missing, duplicate, stale and tampered active heads stop recovery', async () => {
  const fixed = entity('ira_task_checkpoint_v2');
  const head = entity('ira_task_current_v2', 2, 'waiting', 'op-2', headKey);
  for (const heads of [[], [head, head], [entity('ira_task_current_v2', 1, 'running', 'op-1', headKey)]]) {
    await assert.rejects(() => execute({command: 'read', writer, instance, access_key: ''}, network(heads, [fixed])));
  }
  const tampered = structuredClone(head);
  tampered.attributes.find(a => a.name === 'order_id').value = 'attacker';
  await assert.rejects(() => execute({command: 'read', writer, instance, access_key: ''},
    network([tampered], [fixed])), /differs/);
});
test('atomic v2 batch journals a lost send once; restart cannot submit another transaction', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'ira-v2-'));
  const journal = join(directory, 'send.json'); let sends = 0;
  const input = {command: 'write', writer, instance, access_key: '', private_key: privateKey,
    task: 'task-1', revision: '1', previous_revision: '0', encrypted: 'abc', public_state: state,
    journal, max_fee_wei: '10000000000000000'};
  const rpc = network([], [], (method, params) => {
    if (method === 'eth_getCode') return '0x';
    if (method === 'eth_blockNumber') return '0x1';
    if (method === 'eth_getTransactionCount') return '0x0';
    if (method === 'eth_gasPrice') return '0x1';
    if (method === 'eth_getBalance') return '0xde0b6b3a7640000';
    if (method === 'eth_estimateGas') { assert.equal(params[1], 'latest'); return '0x40000'; }
    if (method === 'eth_getBlockByNumber') return {number: '0x1', gasLimit: '0x1c9c380', transactions: []};
    if (method === 'eth_sendRawTransaction') {
      sends++; const saved = JSON.parse(readFileSync(journal));
      assert.equal(saved.state, 'submitting'); assert.equal(saved.operations, 2);
      assert.equal(saved.raw, params[0]); assert.deepEqual(saved.public_state, state);
      throw Error('Lost response');
    }
    if (method === 'eth_getTransactionReceipt') return null;
    throw Error(method);
  });
  try {
    await assert.rejects(() => execute(input, rpc));
    assert.equal(sends, 1);
    await assert.rejects(() => execute(input, rpc));
    assert.equal(sends, 1);
  } finally {rmSync(directory, {recursive: true, force: true});}
});
