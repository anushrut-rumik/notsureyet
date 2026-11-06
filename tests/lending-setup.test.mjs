import {test} from 'node:test';
import assert from 'node:assert/strict';
import {privateKeyToAccount} from 'viem/accounts';
import {buildSetupPlan,createSetupStore,approvalData,matchesTransaction} from '../scripts/lib/lending-setup.mjs';
import {verifySetupExecution} from '../scripts/lib/lending-receipts.mjs';
// Public deterministic test-only key. Never used on a live chain or stored wallet.
const account=privateKeyToAccount('0x'+'01'.padStart(64,'0'));
const artifacts={pool:{abi:[{type:'constructor',inputs:[{type:'address'},{type:'address'},{type:'uint256'},{type:'uint256'}]}],bytecode:{object:'0x6000'}},
 wallet:{abi:[{type:'constructor',inputs:[{type:'address'},{type:'address'},{type:'address'},{type:'address'},{type:'address'},{type:'bytes32'},{type:'bytes32'},{type:'uint256'},{type:'uint256'}]}],bytecode:{object:'0x6001'}}};
const plan=()=>buildSetupPlan(account.address,{'11155111':6,'84532':1},artifacts,Math.floor(Date.now()/1000));
const state=()=>({plan:plan(),approval:null,records:{}});
function client(p){const a=p.actions[0];return {getChainId:async()=>Number(a.chainId),getTransactionCount:async()=>a.nonce,
 getCode:async()=>undefined,estimateGas:async()=>100000n,getTransaction:async()=>({from:a.from,to:null,input:a.data,value:0n,nonce:a.nonce}),
 getTransactionReceipt:async()=>({status:'success',contractAddress:a.creates,blockNumber:10n,blockHash:'0x'+'ab'.repeat(32),gasUsed:100000n,effectiveGasPrice:1n}),
 getBlock:async()=>({hash:'0x'+'ab'.repeat(32)})};}
async function approve(store){await store.approve(await account.signTypedData(approvalData(store.state.plan)));}
test('setup uses owned CREATE addresses, exact reserves and ordered per-chain nonces',()=>{
 const p=plan();assert.equal(p.actions.length,12);
 for(const c of p.chains){const actions=p.actions.filter(a=>a.chainId===c.chainId);assert.deepEqual(actions.map(a=>a.nonce),actions.map((_,i)=>c.startNonce+i));
  assert.equal(actions[0].creates,c.pool);assert.equal(actions[1].creates,c.wallet);}
 assert.equal(p.principalUnits,'800000000000000000');assert.equal(p.protectedIdleReserveUnits,'200000000000000000');
 assert.equal(p.minNetBenefitMicrounits,'1000000');assert.equal(p.maxFeeMicrounits,'3000000');assert.equal(p.actions.at(-1).phase,'hold');
});
test('setup requires independent wallet approval, detects tampered plans and rejects wrong owners',async()=>{
 const s=state(),store=createSetupStore(s,null,()=>client(s.plan));
 await assert.rejects(store.claim(s.plan.actions[0].id),/sign the exact/);
 const other=privateKeyToAccount('0x'+'02'.padStart(64,'0'));
 await assert.rejects(store.approve(await other.signTypedData(approvalData(s.plan))),/not from its owner/);
 await approve(store);s.plan.protectedIdleReserveUnits='0';await assert.rejects(store.claim(s.plan.actions[0].id),/approved plan changed/);
});
test('admission is one-shot across simultaneous requests and uncertain signing cannot retry',async()=>{
 const s=state(),store=createSetupStore(s,null,()=>client(s.plan));await approve(store);
 const results=await Promise.allSettled([store.claim(s.plan.actions[0].id),store.claim(s.plan.actions[0].id)]);
 assert.equal(results.filter(r=>r.status==='fulfilled').length,1);assert.equal(s.records[s.plan.actions[0].id].status,'claimed');
 await assert.rejects(store.claim(s.plan.actions[0].id),/already started/);
 await store.rejected(s.plan.actions[0].id);await store.claim(s.plan.actions[0].id);
});
test('wrong chain, changed nonce, and pre-existing deployment block sending',async()=>{
 for(const mutate of [c=>c.getChainId=async()=>1,c=>c.getTransactionCount=async()=>99,c=>c.getCode=async()=>'0x6000']){
  const s=state(),c=client(s.plan);mutate(c);const store=createSetupStore(s,null,()=>c);await approve(store);
  await assert.rejects(store.claim(s.plan.actions[0].id));assert.deepEqual(s.records,{});
 }
});
test('receipt recovery validates transaction identity and nonzero real hashes before advancing',async()=>{
 const s=state(),c=client(s.plan),store=createSetupStore(s,null,()=>c);await approve(store);const a=s.plan.actions[0];await store.claim(a.id);
 await assert.rejects(store.attach(a.id,'0x'+'00'.repeat(32)),/nonzero/);
 const original=c.getTransaction;c.getTransaction=async()=>({...await original(),value:1n});
 await assert.rejects(store.attach(a.id,'0x'+'11'.repeat(32)),/must not attach ETH/);c.getTransaction=original;
 await store.attach(a.id,'0x'+'11'.repeat(32));c.getCode=async()=>'0x6000';await store.reconcile();
 assert.equal(s.records[a.id].status,'confirmed');assert.equal(s.records[a.id].hash,'0x'+'11'.repeat(32));
});
test('hold-rate action remains locked until deployed move evidence exists',async()=>{
 const s=state();for(const a of s.plan.actions.slice(0,-1))s.records[a.id]={status:'confirmed'};
 const store=createSetupStore(s,null,()=>client(s.plan));await approve(store);
 await assert.rejects(store.claim('hold-rate'),/move dry run must pass/);
});

function relayedClient(action){
 const hash='0x'+'33'.repeat(32),blockHash='0x'+'ab'.repeat(32),relayer='0x'+'77'.repeat(20);
 const receipt={status:'success',blockNumber:10n,blockHash,gasUsed:100000n,effectiveGasPrice:1n};
 const trace=(traceAddress,from,to,input)=>({type:'call',action:{callType:'call',from,to,input,value:'0x0'},
  traceAddress,transactionHash:hash,blockHash,blockNumber:10,result:{output:'0x'}});
 const traces=[trace([],relayer,relayer,'0x1234'),trace([0],relayer,action.from,'0x1234'),trace([0,0],action.from,action.to,action.data)];
 const c={getChainId:async()=>Number(action.chainId),getTransactionCount:async()=>action.nonce,
  getTransaction:async()=>({hash,chainId:Number(action.chainId),from:relayer,to:relayer,input:'0x1234',nonce:999,value:0n,blockHash,blockNumber:10n}),
  getTransactionReceipt:async()=>receipt,getBlock:async()=>({hash:blockHash}),request:async()=>traces,estimateGas:async()=>100000n};
 return {c,hash,traces,receipt};
}

test('step 9 relayed execution recovers and step 10 uses the unconsumed owner nonce',async()=>{
 const s=state(),a=s.plan.actions[8],{c,hash}=relayedClient(a);
 for(const previous of s.plan.actions.slice(0,8))s.records[previous.id]={status:'confirmed'};
 s.records[a.id]={status:'claimed'};
 const store=createSetupStore(s,null,()=>c);await approve(store);
 await store.attach(a.id,hash);await store.reconcile();
 assert.equal(s.records[a.id].status,'confirmed');assert.equal(s.records[a.id].execution.kind,'relayed');
 const originalPlan=JSON.stringify(s.plan),next=s.plan.actions[9];
 const tx=await store.claim(next.id);assert.equal(tx.nonce,'0x3');
 assert.equal(JSON.stringify(s.plan),originalPlan,'Do not mutate the signed plan');
 c.getTransaction=async()=>({from:next.from,to:next.to,input:next.data,value:0n,nonce:3});
 await store.attach(next.id,'0x'+'44'.repeat(32));await store.reconcile();
 c.getTransactionCount=async()=>4;
 assert.equal((await store.claim(s.plan.actions[10].id)).nonce,'0x4');
});

test('relay proof rejects wrong calls, reverted ancestors, duplicate traces and nonce drift',async()=>{
 const a=plan().actions[8];
 for(const mutate of [
  f=>f.traces[2].action.from='0x'+'88'.repeat(20),
  f=>f.traces[2].action.to='0x'+'88'.repeat(20),
  f=>f.traces[2].action.input='0xdeadbeef',
  f=>f.traces[2].action.value='0x1',
  f=>f.traces[2].action.callType='delegatecall',
  f=>f.traces[1].error='Reverted',
  f=>f.traces[2].blockHash='0x'+'ff'.repeat(32),
  f=>f.traces.push({...f.traces[2]}),
  f=>f.receipt.status='reverted',
  f=>f.c.getTransactionCount=async({blockNumber})=>blockNumber===9n?3:4,
  f=>f.c.request=async()=>{throw Error('Trace unavailable');},
 ]){
  const f=relayedClient(a);mutate(f);await assert.rejects(verifySetupExecution(f.c,a,f.hash));
 }
 const f=relayedClient(a);
 await assert.rejects(verifySetupExecution(f.c,{...a,to:null,creates:a.to},f.hash),/deployment must be sent directly/);
});
