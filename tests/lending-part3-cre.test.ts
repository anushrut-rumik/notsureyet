// CRE SDK fault-injection harness. Not live testnet or native simulator delivery evidence.
import {expect} from 'bun:test';
import {test,newTestRuntime,EvmMock,ConsensusMock,addContractMock} from '@chainlink/cre-sdk/test';
import {bigintToProtoBigInt,hexToBase64,hexToBytes,TxStatus} from '@chainlink/cre-sdk';
import {testnets} from '../lending/testnets.js';
import {readAbi} from '../lending/observation.js';
import {onTrigger,executionAbi} from '../cre-lending/part3/workflow.js';
import {buildInstruction} from '../lending/part3/report.js';
import {FIXTURE_NOW} from '../lending/fixtures.js';
// @ts-ignore Node fixture is intentionally outside the WASM bundle.
import {executionFixture,H} from './helpers/lending-part3.mjs';
function setup(change?:'rate'|'fee'|'pending'|'depleted'|'missingHash'|'writeFailure'|'policyCost'){
 const p=executionFixture();
 if(change==='policyCost'){
  p.observation.run.policy.maxFeeMicrounits='1';p.instruction=buildInstruction(p.observation,p.instruction.expiry);
 }
 if(change==='depleted'){
  p.observation.chains[0].values[8]='0';p.observation.run.snapshot.feeBalances[0].availableUnits='0';
  p.instruction=buildInstruction(p.observation,p.instruction.expiry);
 }
 ConsensusMock.testInstance().report=()=>{
  const raw=new Uint8Array(109);raw.set(hexToBytes(p.observation.workflowId),45);raw.set(new TextEncoder().encode('eb6597b43f'),77);
  raw.set(hexToBytes(p.observation.run.context.walletAddress),87);return {rawReport:hexToBase64(('0x'+Array.from(raw,b=>b.toString(16).padStart(2,'0')).join('')) as any)};
 };
 const runtime=newTestRuntime(null,{timeProvider:()=>Number(FIXTURE_NOW)*1000},{mode:'testnet-execution' as const,configVersion:'part3-testnets-v1' as const});
 let writes=0;
 testnets.forEach((n,index)=>{
  const c=p.observation.chains[index],mock=EvmMock.testInstance(BigInt(n.selector));let observeCalls=0;
  mock.headerByNumber=()=>({header:{timestamp:FIXTURE_NOW,blockNumber:bigintToProtoBigInt(10),hash:hexToBase64(c.blockHash),parentHash:hexToBase64('0x'+'00'.repeat(32))}});
  const wallet=addContractMock(mock,{address:c.wallet,abi:readAbi});
  wallet.observe=()=>{observeCalls++;return c.values.map((v:string,k:number)=>BigInt(change==='rate'&&observeCalls>1&&k===3?'0':v));};
  wallet.binding=()=>c.binding;wallet.quoteLink=()=>BigInt(c.quoteLinkUnits);
  const execution=addContractMock(mock,{address:c.wallet,abi:executionAbi});
  execution.quoteOperation=()=>BigInt(change==='fee'?'0':c.quoteLinkUnits);execution.activeOperation=()=>change==='pending'?H:'0x'+'00'.repeat(32);execution.consumed=()=>false;
  addContractMock(mock,{address:n.token,abi:readAbi}).decimals=()=>18;addContractMock(mock,{address:n.link,abi:readAbi}).decimals=()=>18;
  mock.writeReport=()=>{writes++;return {txStatus:change==='writeFailure'?TxStatus.REVERTED:TxStatus.SUCCESS,
    txHash:change==='missingHash'?undefined:hexToBase64(H)};};
 });
 return {runtime,p,trigger:{input:new TextEncoder().encode(JSON.stringify(p))} as any,writes:()=>writes};
}
test('execution sends one authenticated raw report and remains submitted, not completed',()=>{
 const s=setup();const result=JSON.parse(onTrigger(s.runtime,s.trigger));expect(result.state).toBe('submitted');expect(result.sourceTransactionHash).toBe(H);expect(s.writes()).toBe(1);
});
for(const change of ['rate','fee','pending'] as const)test(`fresh ${change} drift cancels without a report write`,()=>{
 const s=setup(change);expect(()=>onTrigger(s.runtime,s.trigger)).toThrow();expect(s.writes()).toBe(0);
});
for(const change of ['policyCost','depleted'] as const)test(`financial rejection ${change} sends zero source transactions`,()=>{
 const s=setup(change);const result=JSON.parse(onTrigger(s.runtime,s.trigger));expect(result.submitted).toBe(false);expect(result.result.decision.action).toBe('blocked');expect(s.writes()).toBe(0);
 expect(result.result.decision.code).toBe(change==='policyCost'?'FEE_LIMIT':'INSUFFICIENT_FEE_BALANCE');
});
for(const change of ['missingHash','writeFailure'] as const)test(`uncertain ${change} cannot be treated as source confirmation`,()=>{
 const s=setup(change);expect(()=>onTrigger(s.runtime,s.trigger)).toThrow();expect(s.writes()).toBe(1);
});
test('receiver-free target has no read, report or write capability',()=>{
 const runtime=newTestRuntime(null,undefined,{mode:'local-simulation' as const,scope:'receiver-free-no-execution' as const});
 const result=JSON.parse(onTrigger(runtime,{input:new Uint8Array()} as any));expect(result.executed).toBe(false);expect(result.broadcast).toBe(false);
});
