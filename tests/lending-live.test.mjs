// Fault injection for the real signer/journal path. RPC and CRE transport are fixtures.
// Keys here are disposable public test vectors, never existing wallet material.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {parseTransaction,decodeFunctionData,parseAbi,keccak256,toBytes,encodeAbiParameters,parseAbiParameters} from 'viem';
import {privateKeyToAccount} from 'viem/accounts';
import {executeLive} from '../scripts/part4/live.mjs';
import {exampleRun} from '../.local/lending-build/fixtures.js';
import {evaluateRun} from '../.local/lending-build/run.js';
import {testnets} from '../.local/lending-build/testnets.js';
import {OperationJournal} from '../scripts/part3/journal.mjs';
const key='0x'+'12'.repeat(32),owner=privateKeyToAccount(key).address,H='0x'+'aa'.repeat(32),ZERO='0x'+'00'.repeat(32);
const abi=parseAbi(['function transfer(address,uint256)','function depositToPool(uint256)',
 'function withdrawToOwner(uint256,uint256)','function setPolicyVersion(bytes32)','function settleOperation(bytes32,bytes32)']);
const artifacts=Object.fromEntries(['ExecutionStrategyWallet','MockPool'].map(n=>[n,JSON.parse(readFileSync(`contracts/out/${n}.sol/${n}.json`))]));
function fixture(t){
 const directory=mkdtempSync(join(tmpdir(),'lending-live-test-'));t.after(()=>rmSync(directory,{recursive:true,force:true}));
 const now=String(Math.floor(Date.now()/1000)),seed=structuredClone(exampleRun(30));
 const policy={...seed.policy,userId:'fixture-user',walletAddress:owner,approvalId:'whatsapp-event:fixture',version:'1',approvedAt:now,
  deadline:String(BigInt(now)+86400n*30n),maxMovableUnits:'800000000000000000',protectedIdleReserveUnits:'200000000000000000'};
 const chains=testnets.map((n,i)=>({chainId:n.chainId,wallet:'0x'+String(i+3).repeat(40),pool:'0x'+String(i+5).repeat(40)}));
 const request={directory,user:policy.userId,key,action:'start',manifest:{scope:'dedicated-test-only',owner,chains,
  workflowId:H,maxTransactionGasWei:'10000000000000000',linkFundingUnits:'1000000000000000000',seedRun:seed},
  plan:{id:'test-plan',status:'approved',approvalEvent:'fixture',policy,settings:{amountUnits:policy.maxMovableUnits,reserveUnits:policy.protectedIdleReserveUnits}}};
 const state={finalized:true,loseResponse:false,gas:21000n,gasPrice:10n,native:10n**18n,reverted:false,sends:[],cre:0,
  chains:new Map(chains.map(c=>[c.chainId,{nonce:0,principal:0n,idle:0n,link:0n,liquidity:100n*10n**18n,
   version:ZERO,active:ZERO,inbound:0n,txs:new Map()}]))};
 const clientFor=id=>{
  const c=chains.find(c=>c.chainId===id),other=chains.find(c=>c.chainId!==id),n=testnets.find(n=>n.chainId===id),s=state.chains.get(id);
  const values=()=>[BigInt(id),10n,BigInt(now),400n*10n**23n,s.principal,s.idle,s.liquidity,100n*10n**18n,s.link,state.native,0n,BigInt(owner)];
  return {
   getChainId:async()=>Number(id),getBlock:async({blockTag})=>({number:blockTag&&!state.finalized?5n:10n,hash:H,timestamp:BigInt(now)}),
   getCode:async({address})=>artifacts[address===c.wallet?'ExecutionStrategyWallet':'MockPool'].deployedBytecode.object,
   readContract:async({functionName})=>({binding:keccak256(encodeAbiParameters(parseAbiParameters('address,address,address,address,address,address,bytes32,bytes32,uint256,uint256,address'),[owner,c.pool,n.token,n.link,n.router,n.forwarder,H,s.version,200000000000000000n,800000000000000000n,other.wallet])),owner,pool:c.pool,asset:n.token,peer:other.wallet,protectedReserve:200000000000000000n,
    balanceOf:10n**20n,maxMove:800000000000000000n,observe:values(),policyVersion:s.version,reservedInbound:s.inbound,activeOperation:s.active})[functionName],
   getTransactionCount:async()=>s.nonce,estimateGas:async()=>state.gas,getGasPrice:async()=>state.gasPrice,getBalance:async()=>state.native,
   getTransaction:async({hash})=>s.txs.get(hash),getTransactionReceipt:async({hash})=>{
    if(!s.txs.has(hash)){const e=Error('missing');e.name='TransactionReceiptNotFoundError';throw e;}
    return {status:state.reverted?'reverted':'success',blockNumber:10n,blockHash:H,transactionHash:hash,gasUsed:21000n,effectiveGasPrice:10n};
   },
   sendRawTransaction:async({serializedTransaction})=>{
    const tx=parseTransaction(serializedTransaction),hash=keccak256(serializedTransaction),call=decodeFunctionData({abi,data:tx.data});
    state.sends.push({chain:id,call,hash});s.txs.set(hash,{...tx,value:tx.value??0n,nonce:tx.nonce??0,hash,input:tx.data,from:owner});s.nonce++;
    if(!state.reverted){
     if(call.functionName==='setPolicyVersion')s.version=call.args[0];
     if(call.functionName==='transfer'){if(tx.to.toLowerCase()===n.link.toLowerCase())s.link+=call.args[1];else s.idle+=call.args[1];}
     if(call.functionName==='depositToPool'){s.idle-=call.args[0];s.principal+=call.args[0];}
     if(call.functionName==='withdrawToOwner'){s.principal-=call.args[0];s.idle-=call.args[1];}
     if(call.functionName==='settleOperation')s.active=ZERO;
    }
    if(state.loseResponse)throw Error('RPC response lost after acceptance');return hash;
   },getLogs:async()=>[],
  };
 };
 return {request,state,clientFor,call:action=>executeLive({...request,action},{clientFor})};
}
test('real signer journals funding, deposit and withdrawal, with no duplicate submission after lost responses',async t=>{
 const f=fixture(t);f.state.loseResponse=true;
 let result;for(let i=0;i<12;i++){result=await f.call('start');if(result.status==='deposit_verified')break;}
 assert.equal(result.status,'deposit_verified');assert.equal(f.state.sends.length,7);
 assert.equal(f.state.chains.get('11155111').principal,800000000000000000n);
 assert.equal(f.state.chains.get('84532').idle,200000000000000000n);
 assert.ok(result.transactions.every(tx=>tx.state==='confirmed'&&tx.receipt.nativeFeeWei==='210000'));
 f.request.plan.status='withdrawing';
 for(let i=0;i<5;i++){result=await f.call('withdraw');if(result.status==='withdrawal_verified')break;}
 assert.equal(result.status,'withdrawal_verified');assert.equal(f.state.sends.length,9);
 assert.ok([...f.state.chains.values()].every(s=>s.principal===0n&&s.idle===0n));
 await f.call('withdraw');assert.equal(f.state.sends.length,9);
});
test('unfinalized receipt blocks every new funding and withdrawal transaction after restart',async t=>{
 const f=fixture(t);f.state.finalized=false;
 assert.equal((await f.call('start')).status,'funding_pending');
 assert.equal((await f.call('start')).status,'pending_transaction');
 f.request.plan.status='withdrawing';assert.equal((await f.call('withdraw')).status,'pending_transaction');
 assert.equal(f.state.sends.length,1);
});
test('wrong sender receipt, reverted transaction and gas cap cannot report completion',async t=>{
 const f=fixture(t);await f.call('start');
 const tx=[...f.state.chains.get('11155111').txs.values()][0];tx.from='0x'+'ff'.repeat(20);
 await assert.rejects(()=>f.call('start'));assert.equal(f.state.sends.length,1);
 tx.from=owner;f.state.reverted=true;await assert.rejects(()=>f.call('withdraw'),/reverted/);
 const g=fixture(t);g.state.gasPrice=10n**18n;await assert.rejects(()=>g.call('start'),/gas cap/);assert.equal(g.state.sends.length,0);
});
test('pause and expiry prevent investment while allowing principal withdrawal',async t=>{
 const f=fixture(t);f.request.plan.status='paused';await assert.rejects(()=>f.call('start'),/stopped/);
 const s=f.state.chains.get('11155111');s.principal=800000000000000000n;s.idle=200000000000000000n;
 f.request.plan.status='expired';f.request.plan.policy.deadline='1';await assert.rejects(()=>f.call('evaluate'),/stopped/);
 assert.equal((await f.call('withdraw')).status,'withdrawal_pending');
 assert.equal((await f.call('withdraw')).status,'withdrawal_verified');
});
test('withdrawal returns only available liquidity and never claims the remainder was returned',async t=>{
 const f=fixture(t),s=f.state.chains.get('11155111');s.principal=800000000000000000n;s.idle=200000000000000000n;s.liquidity=300000000000000000n;
 f.request.plan.status='withdrawing';await f.call('withdraw');assert.equal(s.principal,500000000000000000n);
 s.liquidity=0n;assert.equal((await f.call('withdraw')).status,'withdrawal_pending');assert.equal(f.state.sends.length,1);
 s.liquidity=s.principal;await f.call('withdraw');assert.equal((await f.call('withdraw')).status,'withdrawal_verified');
});
// Build independently consistent policy/binding observations so the real instruction validator runs.
async function observeFor(f,observation){
 const p=structuredClone(observation),now=String(Math.floor(Date.now()/1000)),s=p.run.snapshot;
 p.run.observation.assembledAt=now;
 for(const c of p.chains){
  c.values=c.values.map(String);c.values[2]=now;
  c.values[3]=c.chainId===s.position.location.chainId?'40000000000000000000000000':'100000000000000000000000000000';
  c.quoteLinkUnits='1000000000000000';
  c.binding=keccak256(encodeAbiParameters(parseAbiParameters('address,address,address,address,address,address,bytes32,bytes32,uint256,uint256,address'),[
   owner,c.pool,testnets.find(n=>n.chainId===c.chainId).token,testnets.find(n=>n.chainId===c.chainId).link,
   testnets.find(n=>n.chainId===c.chainId).router,testnets.find(n=>n.chainId===c.chainId).forwarder,H,
   keccak256(toBytes(p.run.policy.version)),200000000000000000n,800000000000000000n,p.chains.find(x=>x!==c).wallet]));
 }
 const source=p.chains.find(c=>c.chainId===s.position.location.chainId),dest=p.chains.find(c=>c!==source);
 const evidence={observedAt:now,source:'fault-injection-only',simulated:true};
 Object.assign(s.position,{investedUnits:source.values[4],evidence});Object.assign(s.reserve,{idleBalanceUnits:source.values[5],evidence});
 Object.assign(s.currentPool,{aprBps:'400',withdrawalAvailableUnits:source.values[4],evidence});Object.assign(s.destinationPool,{aprBps:'1000000',depositCapacityUnits:dest.values[7],evidence});
 for(const c of s.costs.components)Object.assign(c,{assetId:c.category==='sourceGas'?'ETH_GAS':'LINK',assetDecimals:18,
  amountUnits:c.category==='sourceGas'?'1500000':c.category==='bridgeFee'?source.quoteLinkUnits:'0',priceMicrounitsPerToken:'1000000',evidence});
 for(const v of [s.valuation,s.costs.exitIfStay,s.costs.exitIfMove])v.evidence=evidence;
 s.feeBalances=p.chains.flatMap(c=>[['LINK',8],['ETH_GAS',9]].map(([assetId,index])=>({chainId:c.chainId,assetId,assetDecimals:18,availableUnits:c.values[index],reservedUnits:'0',evidence})));
 return {observation:p};
}
test('policy move is signed once, persists unknown before CRE, and pause never resubmits it',async t=>{
 const f=fixture(t);f.request.plan.status='active';
 for(const s of f.state.chains.values()){s.version=keccak256(toBytes('1'));s.idle=200000000000000000n;s.link=10n**18n;}
 f.state.chains.get('11155111').principal=800000000000000000n;
 const deps={clientFor:f.clientFor,refresh:p=>observeFor(f,p),evaluate:async input=>({result:evaluateRun(input,()=>String(Math.floor(Date.now()/1000)))}),
  broadcast:async path=>{f.state.cre++;const p=JSON.parse(readFileSync(path));
   const j=new OperationJournal(join(f.request.directory,'operations.sqlite'));assert.equal(j.get(p.instruction.operationId).state,'submission_unknown');j.close();}};
 const result=await executeLive({...f.request,action:'evaluate'},deps);
 assert.equal(result.status,'submission_unknown');assert.equal(f.state.cre,1);
 f.request.plan.status='paused';assert.equal((await executeLive({...f.request,action:'pause'},deps)).status,'paused');assert.equal(f.state.cre,1);
 const j=new OperationJournal(join(f.request.directory,'operations.sqlite'));assert.equal(j.list().length,1);assert.equal(j.list()[0].state,'submission_unknown');j.close();
});
test('withdrawal settles the matching recovered source operation, ignoring older completed operations',async t=>{
 const f=fixture(t);f.request.plan.status='withdrawing';
 const s=f.state.chains.get('11155111');s.idle=200000000000000000n;
 const journal=new OperationJournal(join(f.request.directory,'operations.sqlite'));
 for(const name of ['old','recovered']){
  const p=await observeFor(f,{workflowId:H,chains:f.request.manifest.chains.map(c=>({...c,values:[c.chainId,'10',String(Math.floor(Date.now()/1000)),'0',c.chainId==='11155111'?'800000000000000000':'0','200000000000000000','100000000000000000000','100000000000000000000','1000000000000000000','1000000000000000000','0',String(BigInt(owner))],blockNumber:'10',blockHash:H,tokenDecimals:18,linkDecimals:18})),run:structuredClone(f.request.manifest.seedRun)});
  // Only journal settlement selection is under test here; immutable payload integrity still checked.
  const payload={observation:p.observation,instruction:{operationId:keccak256(toBytes(name)),sourceChainId:'11155111'},signature:'fixture'};
  journal.prepare(payload);journal.claim(payload.instruction.operationId);journal.update(payload.instruction.operationId,'source_confirmed',{messageId:H});
  journal.update(payload.instruction.operationId,'destination_executed');journal.update(payload.instruction.operationId,'idle_tokens');journal.update(payload.instruction.operationId,'recovered_idle');
  if(name==='recovered')s.active=payload.instruction.operationId;
 }
 journal.close();await f.call('withdraw');
 assert.equal(f.state.sends[0].call.functionName,'settleOperation');assert.equal(f.state.sends[0].call.args[0],keccak256(toBytes('recovered')));
});
test('a scheduled hold still invokes CRE evaluation and never calls broadcast',async t=>{
 const f=fixture(t);f.request.plan.status='active';f.state.chains.get('11155111').principal=800000000000000000n;
 for(const s of f.state.chains.values()){s.version=keccak256(toBytes('1'));s.idle=200000000000000000n;s.link=10n**18n;}
 let calls=0;
 const result=await executeLive({...f.request,action:'evaluate'},{clientFor:f.clientFor,
  refresh:async p=>{const refreshed=await observeFor(f,p);refreshed.observation.run.snapshot.destinationPool.aprBps='400';return refreshed;},
  evaluate:async input=>{calls++;return {result:evaluateRun(input,()=>String(Math.floor(Date.now()/1000)))};},
  broadcast:async()=>assert.fail('hold must not broadcast')});
 assert.equal(result.status,'no_move');assert.equal(calls,1);assert.equal(f.state.sends.length,0);
});
test('pause cancels an unsigned prepared operation without contacting a broadcaster',async t=>{
 const f=fixture(t);f.request.plan.status='paused';
 const j=new OperationJournal(join(f.request.directory,'operations.sqlite'));
 j.prepare({instruction:{operationId:H,expiry:String(Math.floor(Date.now()/1000)+300)},observation:{run:{context:{walletAddress:owner}}}});j.close();
 const result=await f.call('pause');assert.equal(result.status,'paused');assert.equal(result.operations[0].state,'cancelled');assert.equal(f.state.sends.length,0);
});
