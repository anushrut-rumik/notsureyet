// Separate encrypted activity publications. Never imports or invokes CRE execution.
import {createPublicClient, ExpirationTime} from '@arkiv-network/sdk';
import {defineSchema, str, bytes, key} from '@arkiv-network/sdk/schema';
import {and, or, eq, gt, asc} from '@arkiv-network/sdk/query';
import {rabdi} from '@arkiv-network/sdk/chains';
import {http, custom} from 'viem';
import {privateKeyToAccount} from 'viem/accounts';
import {readFile, mkdir, open} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {prepareImport, runImport} from './arkiv-import.mjs';

export const Activity = defineSchema('ira_lending_activity_encrypted_v1', {
  scope: str().indexed(), event: str().indexed(), encrypted: bytes(),
});
const opaque = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const check = (ok, message) => {if (!ok) throw Error(message);};
export function validateRow(row) {
  check(opaque(row.scope) && opaque(row.event), 'Invalid opaque activity identity');
  check(typeof row.encrypted === 'string' && /^[A-Za-z0-9+/]+={0,2}$/.test(row.encrypted)
    && row.encrypted.length >= 40 && row.encrypted.length <= 100000, 'Invalid encrypted activity');
  return {scope:row.scope, event:row.event, encrypted:Buffer.from(row.encrypted)};
}
export async function readActivity(reader, writer, scope, after, events) {
  check(opaque(scope), 'Invalid activity scope');
  const filters=[eq('scope',str(scope))];
  if(after) filters.push(gt('$key',key(after)));
  if(events) {
    check(Array.isArray(events) && events.length>0 && events.length<=200 && events.every(opaque),'Invalid activity page');
    filters.push(or(...events.map(value=>eq('event',str(value)))));
  }
  const result=await reader.select('*').from(Activity,{onMismatch:'throw'}).createdBy(writer)
    .ownedBy(writer).where(and(...filters)).orderBy(asc('$key')).limit(100).fetch();
  return result.entities.map(row=>{
    check(row.$creator.toLowerCase()===writer.toLowerCase() && row.$owner.toLowerCase()===writer.toLowerCase()
      && row.$creationFlags?.readonly===true && row.scope.value===scope, 'Untrusted activity publisher');
    const item={scope:row.scope.value,event:row.event.value,
      encrypted:Buffer.from(row.encrypted.value.slice(2),'hex').toString()};
    validateRow(item);
    return {...item,entity_key:row.$key,expires_at_block:row.$expiresAt.toString()};
  });
}
export async function execute(input, rpcOverride) {
  const chain=rabdi({accessKey:input.access_key ?? ''});
  const transport=http(undefined,{batch:{batchSize:20,wait:10},retryCount:0,timeout:15000,
    fetchOptions:{cache:'no-store',redirect:'error'}});
  const rpc=rpcOverride ?? transport({chain});
  const reader=createPublicClient({chain,transport:custom(rpc,{retryCount:0}),cacheTime:0});
  check(await reader.getChainId()===7794095,'Wrong activity network');
  if(input.command==='read') return {rows:await readActivity(reader,input.writer,input.scope,input.after,input.events)};
  if(input.command==='funding') return {balance_wei:String(await reader.getBalance({address:input.writer}))};
  check(['prepare','publish'].includes(input.command),'Unknown activity command');
  const account=privateKeyToAccount(input.private_key);
  check(account.address.toLowerCase()===input.writer.toLowerCase(),'Activity signer mismatch');
  const directory=resolve(input.directory);
  await mkdir(directory,{recursive:true,mode:0o700});
  const planPath=resolve(directory,'plan.json'), journalPath=resolve(directory,'journal.jsonl');
  const options={account,chain,request:args=>rpc.request(args),planPath,journalPath};
  if(input.command==='prepare') {
    check(!existsSync(planPath) && !existsSync(journalPath),'Existing activity plan must be reconciled');
    check(Array.isArray(input.rows) && input.rows.length>0 && input.rows.length<=20,'Invalid activity batch');
    const head=await reader.getBlockNumber({cacheTime:0});
    const expiresAt=head+ExpirationTime.fromDays(30).minLifetime;
    const rows=input.rows.map(row=>({id:row.event,schema:Activity,values:validateRow(row),
      salt:BigInt('0x'+row.event.slice(0,32)),expiresAt,flags:{readonly:true}}));
    const result=await prepareImport({...options,rows,jobId:input.job_id,batchSize:rows.length,
      fees:{gasPrice:await reader.getGasPrice()},gasCeiling:16777216n,maxTotalCost:BigInt(input.max_fee_wei)});
    // Persist the trusted plan hash separately before the caller can start publishing.
    const fd=await open(resolve(directory,'prepared.json'),'wx',0o600);
    try {await fd.writeFile(JSON.stringify(result));await fd.sync();} finally {await fd.close();}
    return result;
  }
  const result=await runImport({...options,planSha256:input.plan_hash});
  if(result.state!=='complete') return result;
  const plan=JSON.parse(await readFile(planPath,'utf8'));
  const journal=(await readFile(journalPath,'utf8')).trim().split('\n').map(JSON.parse);
  return {...result,rows:plan.batches.flatMap(batch=>batch.rows.map(row=>({event:row.id,
    entity_key:row.expectedKey,expires_at_block:row.expiresAt,
    transaction_hash:journal.find(entry=>entry.index===batch.index && entry.state==='verified').txHash})))};
}
async function main(){
  try {
    let raw='';for await(const c of process.stdin){raw+=c;check(raw.length<2200000,'Activity request too large');}
    const result=await execute(JSON.parse(raw));
    process.stdout.write(JSON.stringify({ok:true,result}));
  } catch {
    process.stdout.write(JSON.stringify({ok:false,error:'Activity sync pending. Reconcile saved storage intent; CRE execution is independent.'}));
    process.exitCode=1;
  }
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) await main();
