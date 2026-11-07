import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execute,validateRow} from '../integrations/lending-activity.mjs';
import {privateKeyToAccount} from 'viem/accounts';
import {verifyReadback} from '../integrations/arkiv-import.mjs';

const privateKey='0x'+'11'.repeat(32),writer=privateKeyToAccount(privateKey).address;
const scope='ab'.repeat(32),event='cd'.repeat(32),encrypted=Buffer.from('encrypted-test-fixture-32-bytes-long').toString('base64');
test('encrypted schema has only opaque lookup fields and bytes',()=>{
  assert.deepEqual(Object.keys(validateRow({scope,event,encrypted})),['scope','event','encrypted']);
  assert.throws(()=>validateRow({scope:'phone-number',event,encrypted}));
});
test('Arkiv reads validate immutable creator scope before returning encrypted bytes',async()=>{
  let mutable=false;
  const rpc={request:async({method,params})=>{
    if(method==='eth_chainId')return '0x76edaf';
    if(method==='arkiv_query'){
      const query=JSON.stringify(params);assert.match(query,/creator/);assert.match(query,/scope/);
      return {blockNumber:'0x1',data:[{key:'0x'+'aa'.repeat(32),owner:writer,creator:writer,expiresAt:'0xffff',
        creationFlags:{readonly:!mutable,permissionlessExtension:false,raw:mutable?0:1},attributes:[
          {name:'arkiv.schema',type:'str',value:'ira_lending_activity_encrypted_v1'},
          {name:'scope',type:'str',value:scope},{name:'event',type:'str',value:event},
          {name:'encrypted',type:'bytes',value:'0x'+Buffer.from(encrypted).toString('hex')}]}]};
    }
    throw Error(method);
  }};
  const input={command:'read',writer,scope};
  assert.equal((await execute(input,rpc)).rows[0].encrypted,encrypted);
  mutable=true;await assert.rejects(()=>execute(input,rpc),/Untrusted/);
});
test('unsigned preparation and a lost send preserve the exact journal and never resend',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'lending-activity-'));
  const methods=[];
  const rpc={request:async({method,params})=>{
    methods.push(method);
    if(method==='eth_chainId')return '0x76edaf';
    if(method==='eth_blockNumber')return '0x64';
    if(method==='eth_getBlockByNumber')return {number:'0x64',hash:'0x'+'12'.repeat(32),gasLimit:'0x1c9c380',gasUsed:'0x0',timestamp:'0x1',transactions:[]};
    if(method==='eth_getTransactionCount')return '0x0';
    if(method==='eth_gasPrice')return '0x1';
    if(method==='eth_getBalance')return '0xde0b6b3a7640000';
    if(method==='eth_estimateGas')return '0x30d40';
    if(method==='eth_call')return '0x'+'00'.repeat(32);
    if(method==='eth_sendRawTransaction'){
      const journal=readFileSync(join(directory,'journal.jsonl'),'utf8');
      assert.ok(journal.includes(params[0]));assert.match(journal,/submitting/);
      throw Error('Lost response');
    }
    if(method==='eth_getTransactionByHash')return null;
    throw Error('Unexpected method '+method);
  }};
  const input={writer,private_key:privateKey,directory,job_id:'fixture',rows:[{scope,event,encrypted}],max_fee_wei:'10000000000000000'};
  try{
    const prepared=await execute({...input,command:'prepare'},rpc);
    assert.ok(!methods.includes('eth_sendRawTransaction'));
    const published=await execute({...input,command:'publish',plan_hash:prepared.planSha256},rpc);
    assert.equal(published.state,'needs_reconciliation');
    await assert.rejects(()=>execute({...input,command:'publish',plan_hash:prepared.planSha256},rpc));
    assert.equal(methods.filter(x=>x==='eth_sendRawTransaction').length,1);
    const original=readFileSync(join(directory,'plan.json'),'utf8');
    writeFileSync(join(directory,'plan.json'),original+' ');
    await assert.rejects(()=>execute({...input,command:'publish',plan_hash:prepared.planSha256},rpc),/tampered/);
  }finally{rmSync(directory,{recursive:true,force:true});}
});
test('batched readback verifies every key and rejects missing or altered encrypted data',async()=>{
  const keys=['0x'+'aa'.repeat(32),'0x'+'bb'.repeat(32)];
  const columns={scope:{type:'str',indexed:true},event:{type:'str',indexed:true},encrypted:{type:'bytes',indexed:false}};
  const rows=keys.map((_,i)=>({schema:{tag:'ira_lending_activity_encrypted_v1',columns},
    values:{scope:{type:'str',value:scope},event:{type:'str',value:i?'ef'.repeat(32):event},
      encrypted:{type:'bytes',value:'0x'+Buffer.from(encrypted).toString('hex')}},
    expiresAt:'1000',flags:{readonly:true,permissionlessExtension:false}}));
  let entities=rows.map((row,i)=>({$key:keys[i],$creator:writer,$owner:writer,$createdAt:10n,$expiresAt:1000n,
    $creationFlags:row.flags,$attributes:{'arkiv.schema':{type:'str',value:row.schema.tag},...row.values},
    $attributeSchema:{'arkiv.schema':{type:'str',indexed:true},...columns}}));
  let reads=0;
  const builder={where(){return this},limit(){return this},async fetch(){reads++;return {blockNumber:11n,entities}}};
  const reader={async getBlockNumber(){return 11n},select(){return builder}};
  const verify=()=>verifyReadback(reader,{account:writer},{rows},{keys,blockNumber:'10'});
  assert.deepEqual(await verify(),['11','11']);assert.equal(reads,1);
  const original=structuredClone(entities);
  entities[1].$attributes.encrypted={type:'bytes',value:'0x00'};
  await assert.rejects(verify,/attributes/);
  entities=original.slice(0,1);await assert.rejects(verify,/missing/);
});
