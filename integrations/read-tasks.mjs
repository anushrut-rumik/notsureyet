// Independent Arkiv reader: no application API, wallet key or signing required.
import { execute } from './arkiv.mjs';
const [writer, instance] = process.argv.slice(2);
if (!/^0x[0-9a-fA-F]{40}$/.test(writer ?? '') || !instance) {
  console.error('Usage: node --env-file=.env integrations/read-tasks.mjs WRITER_ADDRESS INSTANCE_ID');
  process.exitCode = 1;
} else {
  try {
    const result = await execute({command:'read', writer, instance,
      access_key:process.env.AGENT_ARKIV_ACCESS_KEY ?? ''});
    console.log(JSON.stringify(result.rows.map(({encrypted, ...metadata})=>metadata),null,2));
  } catch {
    console.error('Arkiv read failed. Check network, access key and query scope.');process.exitCode=1;
  }
}
