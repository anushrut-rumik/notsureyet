// Mock RPC fault injection only. No network, signing or broadcast.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {encodeFunctionData,parseAbi} from 'viem';
import {tracking} from '../scripts/part4/tracking.mjs';
import {OperationJournal} from '../scripts/part3/journal.mjs';
import {executionFixture,H,sourceReceipt} from './helpers/lending-part3.mjs';
import {verifySourceReceipt} from '../scripts/part3/monitor.mjs';
const depositAbi=parseAbi(['function depositToPool(uint256)']);
function setup(t){
 const p=executionFixture(),directory=mkdtempSync(join(tmpdir(),'lending-part4-'));
 t.after(()=>rmSync(directory,{recursive:true,force:true}));
 const manifest={scope:'dedicated-test-only',owner:p.observation.run.context.walletAddress,
  authenticatedUserId:p.observation.run.context.userId,chains:structuredClone(p.observation.chains)};
 return {p,manifest,directory};
}
function rpc(manifest,receipt={status:'success',blockNumber:20n,blockHash:H}){
 const data=manifest.chains[0].fundingCalls?.[0]?.data;
 return chain=>({getChainId:async()=>Number(chain),getBlock:async()=>({number:100n,hash:H}),
  getTransactionReceipt:async()=>receipt,getTransaction:async()=>({from:manifest.owner,to:manifest.chains[0].wallet,
   input:data,value:0n,chainId:11155111}),getLogs:async()=>[]});
}
test('initial deposit confirmation reuses exact Part 2 call and canonical receipt checks',async t=>{
 const {manifest}=setup(t),chain=manifest.chains[0];
 chain.fundingCalls=[{to:chain.wallet,hash:H,data:encodeFunctionData({abi:depositAbi,functionName:'depositToPool',args:[800n]})}];
 const result=await tracking(manifest,rpc(manifest));
 assert.equal(result.initialDeposits[0].state,'confirmed');assert.equal(result.initialDeposits[0].amountUnits,'800');
 const get=rpc(manifest);await assert.rejects(()=>tracking(manifest,c=>({...get(c),getTransaction:async()=>({...await get(c).getTransaction(),to:'0x'+'99'.repeat(20)})})),/target/);
});
test('dedicated testnet deposit does not wait for finality; reverted receipt is never confirmation',async t=>{
 const {manifest}=setup(t),chain=manifest.chains[0];
 chain.fundingCalls=[{to:chain.wallet,hash:H,data:encodeFunctionData({abi:depositAbi,functionName:'depositToPool',args:[800n]})}];
 const get=rpc(manifest);
 const result=await tracking(manifest,c=>({...get(c),getBlock:async ({blockTag})=>({number:blockTag?1n:20n,hash:H})}));
 assert.equal(result.initialDeposits[0].state,'confirmed');
 const strict={...manifest,scope:'standalone'};
 const waiting=await tracking(strict,c=>({...get(c),getBlock:async ({blockTag})=>({number:blockTag?1n:20n,hash:H})}));
 assert.equal(waiting.initialDeposits[0].state,'pending');
 await assert.rejects(()=>tracking(manifest,rpc(manifest,{status:'reverted',blockNumber:20n,blockHash:H})),/reverted/);
});
test('Part 3 journal monitoring binds exact user, owner and both strategy deployments',async t=>{
 const {p,manifest,directory}=setup(t);manifest.operationJournal=join(directory,'operations.sqlite');
 const journal=new OperationJournal(manifest.operationJournal);journal.prepare(p);journal.close();
 const result=await tracking(manifest,rpc(manifest));assert.equal(result.operations[0].state,'prepared');
 await assert.rejects(()=>tracking({...manifest,authenticatedUserId:'other-user'},rpc(manifest)),/user binding/);
 const changed=structuredClone(manifest);changed.chains[0].wallet='0x'+'99'.repeat(20);
 await assert.rejects(()=>tracking(changed,rpc(manifest)),/deployment binding/);
});
test('Part 3 source receipt retains actual fee-token and gas quantities, separate from projections',()=>{
 const p=executionFixture(),receipt=sourceReceipt(p,{gasUsed:12345n,effectiveGasPrice:999n});
 const result=verifySourceReceipt({immutable:p},receipt);
 assert.equal(result.actualLinkFeeUnits,p.instruction.maxLinkFee);
 assert.equal(result.sourceGasUsed,'12345');assert.equal(result.sourceEffectiveGasPriceWei,'999');
});
