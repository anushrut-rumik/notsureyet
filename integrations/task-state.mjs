// One lifecycle for every capability. The mutable index is verified against readonly history.
import { defineSchema, str, bytes, u64, key } from '@arkiv-network/sdk/schema';
import { eq, and, gt, asc } from '@arkiv-network/sdk/query';
import { ExpirationTime } from '@arkiv-network/sdk';

export const publicFields = ['status', 'capability', 'protocol', 'source_network',
  'destination_network', 'reference', 'source_tx', 'destination_tx', 'order_id', 'progress'];
const columns = () => ({ instance: str().indexed(), task: str().indexed(), revision: u64().indexed(),
  operation: str().indexed(), ...Object.fromEntries(publicFields.map(name => [name, str().indexed()])),
  encrypted: bytes() });
export const CurrentTasks = defineSchema('ira_task_current_v2', columns());
export const Checkpoints = defineSchema('ira_task_checkpoint_v2', columns());
const terminal = status => ['completed', 'cancelled'].includes(status);

export async function scan(reader, schema, writer, instance, task, metadataOnly = false) {
  const rows = []; let last;
  for (let i = 0; i < 50; i++) {
    const filters = [eq('instance', str(instance))];
    if (task) filters.push(eq('task', str(task)));
    if (last) filters.push(gt('$key', key(last)));
    const selection = metadataOnly
      ? { $key: true, $owner: true, $creator: true, $creationFlags: true, $expiresAt: true,
          instance: true, task: true, revision: true, operation: true, status: true }
      : '*';
    const page = await reader.select(selection).from(schema, { onMismatch: 'throw' })
      .createdBy(writer).ownedBy(writer).where(and(...filters)).orderBy(asc('$key')).limit(100).fetch();
    for (const row of page.entities) {
      if (row.$creator.toLowerCase() !== writer.toLowerCase()
        || row.$owner.toLowerCase() !== writer.toLowerCase() || row.instance.value !== instance
        || row.$creationFlags.readonly !== (schema === Checkpoints)) throw Error('Untrusted task record');
      rows.push(row);
    }
    if (page.entities.length < 100) return rows;
    const next = page.entities.at(-1).$key;
    if (last && next <= last) throw Error('Task query did not advance');
    last = next;
  }
  throw Error('Task page limit reached; incomplete state cannot be used');
}

export function dto(row, checkpointKey) {
  return { entity_key: checkpointKey ?? row.$key, current_key: row.$key, schema_version: 2,
    task: row.task.value, revision: row.revision.value.toString(), operation: row.operation.value,
    encrypted: Buffer.from(row.encrypted.value.slice(2), 'hex').toString(),
    expires_at_block: row.$expiresAt.toString(),
    public_state: Object.fromEntries(publicFields.map(name => [name, row[name].value])) };
}

export async function readCurrent(reader, writer, instance, task, readLegacy) {
  // Resolve all revisions BEFORE filtering status. A stale mutable index is never executable state.
  const heads = await scan(reader, CurrentTasks, writer, instance, task);
  const history = await scan(reader, Checkpoints, writer, instance, task, true);
  const headByTask = new Map(); const latest = new Map(); const closed = new Map();
  for (const row of heads) {
    if (headByTask.has(row.task.value)) throw Error('Duplicate current task record');
    headByTask.set(row.task.value, row);
  }
  for (const row of history) {
    const id = row.task.value;
    const prior = latest.get(id);
    if (prior && prior.revision.value === row.revision.value && prior.operation.value !== row.operation.value)
      throw Error('Conflicting task revisions');
    if (!prior || row.revision.value > prior.revision.value) latest.set(id, row);
    if (terminal(row.status.value)) {
      const old = closed.get(id);
      if (!old || row.revision.value > old.revision.value) closed.set(id, row);
    }
  }
  const rows = [];
  for (const id of new Set([...latest.keys(), ...headByTask.keys()])) {
    const head = headByTask.get(id); const checkpoint = closed.get(id) ?? latest.get(id);
    if (closed.has(id)) {
      // A retained terminal checkpoint wins even if an old mutable head is replayed or deleted.
      const fixed = await reader.getEntity(checkpoint.$key, Checkpoints);
      if (fixed.$creationFlags.readonly !== true || fixed.$creator.toLowerCase() !== writer.toLowerCase()
        || fixed.$owner.toLowerCase() !== writer.toLowerCase()) throw Error('Untrusted terminal checkpoint');
      const result = dto(fixed, fixed.$key); result.current_key = head?.$key ?? null; rows.push(result); continue;
    }
    if (!head || !checkpoint || head.revision.value !== checkpoint.revision.value
      || head.operation.value !== checkpoint.operation.value) throw Error('Current task and checkpoint disagree');
    const fixed = await reader.getEntity(checkpoint.$key, Checkpoints);
    for (const name of ['instance', 'task', 'revision', 'operation', 'encrypted', ...publicFields])
      if (head[name].value !== fixed[name].value) throw Error('Current task differs from immutable checkpoint');
    rows.push(dto(head, checkpoint.$key));
  }
  const legacy = await readLegacy();
  const migrated = new Set([...latest.keys(), ...headByTask.keys()]);
  return [...rows, ...legacy.filter(row => !migrated.has(row.task))];
}

export async function prepareBatch(reader, input, operation) {
  const state = input.public_state;
  if (!state || publicFields.some(name => typeof state[name] !== 'string'
    || Buffer.byteLength(state[name]) > 128)) throw Error('Invalid public task state');
  if (!['queued', 'running', 'waiting', 'needs_input', 'completed', 'cancelled'].includes(state.status))
    throw Error('Invalid task status');
  const heads = await scan(reader, CurrentTasks, input.writer, input.instance, input.task);
  if (heads.length > 1) throw Error('Duplicate current task record');
  const head = heads[0];
  if (head && (BigInt(input.revision) <= head.revision.value
    || (terminal(head.status.value) && state.status !== head.status.value))) throw Error('Stale or reopened task');
  const values = { instance: input.instance, task: input.task, revision: u64(BigInt(input.revision)),
    operation, ...state, encrypted: Buffer.from(input.encrypted) };
  const expiration = ExpirationTime.fromDays(terminal(state.status) ? 180 : 30);
  const creates = [{ schema: Checkpoints, values, expiration, flags: { readonly: true },
    salt: BigInt(operation) & ((1n << 128n) - 1n) }];
  if (!head) creates.push({ schema: CurrentTasks, values, expiration, flags: { readonly: false } });
  const batch = { creates };
  if (head) {
    batch.patches = [{ schema: CurrentTasks, entityKey: head.$key, set: values }];
    batch.extensions = [{ entityKey: head.$key, expiration }];
  }
  return { batch, operations: head ? 3 : 2 };
}
