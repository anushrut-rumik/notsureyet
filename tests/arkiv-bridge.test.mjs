import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { privateKeyToAccount } from 'viem/accounts';
import { gasCap, trusted, execute } from '../integrations/arkiv.mjs';

const privateKey='0x'+'11'.repeat(32);
const writer=privateKeyToAccount(privateKey).address;
const key='0x'+'ab'.repeat(32);
const instance='demo';
function row() {
  return {key,owner:writer,creator:writer,expiresAt:'0xffff',creationFlags:{readonly:true,permissionlessExtension:false,raw:1},attributes:[
    {name:'arkiv.schema',type:'str',value:'ira_task_checkpoint_v1'},
    {name:'instance',type:'str',value:instance},{name:'task',type:'str',value:'task-1'},
    {name:'revision',type:'u64',value:'0x1'},{name:'operation',type:'str',value:'op-1'},
    {name:'encrypted',type:'bytes',value:'0x616263'}]};
}
test('gas policy and trusted immutable publisher checks',()=>{
  assert.equal(gasCap(200000n,30000000n),1000000n);
  assert.equal(gasCap(900000n,30000000n),1800000n);
  assert.throws(()=>gasCap(9000000n,30000000n));
  const value={$creator:writer,$owner:writer,$creationFlags:{readonly:true},instance:{value:instance}};
  assert.ok(trusted(value,writer,instance));
  assert.ok(!trusted({...value,$creationFlags:{readonly:false}},writer,instance));
  assert.ok(!trusted({...value,$creator:'0x'+'22'.repeat(20)},writer,instance));
});
test('real SDK query renders indexed instance plus protected creator and owner filters',async()=>{
  let query;
  const rpc={request:async({method,params})=>{
    if(method==='eth_chainId')return '0x76edaf';
    if(method==='arkiv_query'){query=JSON.stringify(params);return {blockNumber:'0x1',
      data:query.includes('ira_task_checkpoint_v1')?[row()]:[]};}
    throw Error(method);
  }};
  const result=await execute({command:'read',writer,instance,access_key:''},rpc);
  assert.equal(result.rows[0].encrypted,'abc');
  assert.equal(result.rows[0].task,'task-1');
  assert.match(query,/ira_task_checkpoint_v1/);
  assert.match(query,/creator/i);
  assert.match(query,/owner/i);
  assert.match(query,/instance/);
});
test('wrong network and mutable copied records are rejected',async()=>{
  const rpc={request:async({method,params})=>{
    if(method==='eth_chainId')return '0x76edaf';
    if(!JSON.stringify(params).includes('ira_task_checkpoint_v1'))return {blockNumber:'0x1',data:[]};
    const entity=row();entity.creationFlags.readonly=false;entity.creationFlags.raw=0;
    return {blockNumber:'0x1',data:[entity]};
  }};
  await assert.rejects(()=>execute({command:'read',writer,instance,access_key:''},rpc),/Untrusted/);
  await assert.rejects(()=>execute({command:'read',writer,instance,access_key:''},
    {request:async()=> '0x1'}),/Wrong Arkiv network/);
});
test('failed exact unsigned estimate never reaches signing or send admission',async()=>{
  const dir=mkdtempSync(join(tmpdir(),'ira-arkiv-'));const methods=[];
  try {
    await assert.rejects(()=>execute({command:'write',writer,instance,private_key:privateKey,access_key:'',
      task:'task-1',revision:'1',encrypted:'abc',journal:join(dir,'send.json'),max_fee_wei:'10000000000000000'},
      {request:async({method})=>{methods.push(method);if(method==='eth_chainId')return '0x76edaf';throw Error('Denied');}}));
    assert.ok(!methods.some(m=>m.startsWith('eth_send')));
  } finally {rmSync(dir,{recursive:true,force:true});}
});
test('lost send response is journaled before forwarding and never resubmitted',async()=>{
  const dir=mkdtempSync(join(tmpdir(),'ira-arkiv-'));const methods=[];
  const journal=join(dir,'send.json');
  const input={command:'write',writer,instance,private_key:privateKey,access_key:'',
    task:'task-1',revision:'1',encrypted:'abc',journal,max_fee_wei:'10000000000000000'};
  const rpc={request:async({method,params})=>{
    methods.push(method);
    if(method==='eth_chainId')return '0x76edaf';
    if(method==='eth_blockNumber')return '0x1';
    if(method==='eth_estimateGas') {assert.equal(params[1],'latest');return '0x30d40';}
    if(method==='eth_getBlockByNumber')return {number:'0x1',hash:'0x'+'00'.repeat(32),gasLimit:'0x1c9c380',gasUsed:'0x0',timestamp:'0x1',transactions:[]};
    if(method==='eth_getTransactionCount')return '0x0';
    if(method==='eth_getCode')return '0x';
    if(method==='eth_gasPrice')return '0x1';
    if(method==='eth_getBalance')return '0xde0b6b3a7640000';
    if(method==='eth_sendRawTransaction') {
      const saved=JSON.parse(readFileSync(journal,'utf8'));
      assert.equal(saved.state,'submitting');assert.ok(saved.hash);assert.equal(saved.raw,params[0]);
      throw Error('Network response lost');
    }
    if(method==='eth_getTransactionReceipt')return null;
    throw Error(method);
  }};
  try {
    await assert.rejects(()=>execute(input,rpc));
    assert.equal(methods.filter(m=>m==='eth_sendRawTransaction').length,1);
    const saved=JSON.parse(readFileSync(journal,'utf8'));assert.equal(saved.state,'submitting');
    await assert.rejects(()=>execute(input,rpc));
    assert.equal(methods.filter(m=>m==='eth_sendRawTransaction').length,1);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test('external wallet prepares without a key, admits one request, and verifies exact transaction and readback',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'ira-arkiv-external-'));
 const journal=join(dir,'send.json');const methods=[];let altered=false;
 const input={command:'prepare_external',writer,instance,access_key:'',task:'task-1',revision:'1',
  encrypted:'abc',journal,max_fee_wei:'10000000000000000'};
 const hash='0x'+'cd'.repeat(32);
 const rpc={request:async({method,params})=>{
  methods.push(method);
  if(method==='eth_chainId')return '0x76edaf';
  if(method==='eth_blockNumber')return '0x1';
  if(method==='eth_getCode')return '0x';
  if(method==='eth_estimateGas')return '0x30d40';
  if(method==='eth_getBlockByNumber')return {number:'0x1',hash:'0x'+'00'.repeat(32),gasLimit:'0x1c9c380',gasUsed:'0x0',timestamp:'0x1',transactions:[]};
  if(method==='eth_getTransactionCount')return '0x0';
  if(method==='eth_gasPrice')return '0x1';
  if(method==='eth_getBalance')return '0xde0b6b3a7640000';
  if(method==='eth_getTransactionByHash'){
   const saved=JSON.parse(readFileSync(journal,'utf8'));
   return {...saved.transaction,hash,input:altered?'0x1234':saved.transaction.data,type:'0x0',blockNumber:'0x1',transactionIndex:'0x0'};
  }
  if(method==='eth_getTransactionReceipt')return {transactionHash:hash,status:'0x1',blockNumber:'0x1',logs:[]};
  if(method==='arkiv_query'){
   const saved=JSON.parse(readFileSync(journal,'utf8'));const entity=row();
   entity.attributes.find(a=>a.name==='operation').value=saved.operation;
   return {blockNumber:'0x1',data:[entity]};
  }
  throw Error(method);
 }};
 try{
  assert.deepEqual(await execute(input,rpc),{pending:true});
  assert.ok(!methods.some(m=>m.startsWith('eth_send')),'preparation must never reach the network send methods');
  const claimed=await execute({...input,command:'claim_external'},rpc);
  assert.equal(claimed.transaction.from.toLowerCase(),writer.toLowerCase());
  assert.equal(claimed.transaction.chainId,'0x76edaf');
  await assert.rejects(()=>execute({...input,command:'claim_external'},rpc),/already started/);
  altered=true;
  await assert.rejects(()=>execute({...input,command:'attach_external',tx_hash:hash},rpc),/differs/);
  altered=false;
  const result=await execute({...input,command:'attach_external',tx_hash:hash},rpc);
  assert.equal(result.entity_key,key);
  assert.equal(JSON.parse(readFileSync(journal,'utf8')).state,'verified');
  assert.ok(!methods.some(m=>m.startsWith('eth_send')));
 }finally{rmSync(dir,{recursive:true,force:true});}
});
