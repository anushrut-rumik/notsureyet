// SDK harness tests, explicitly NOT actual CRE simulator evidence.
import {expect} from 'bun:test';
import {test,newTestRuntime,EvmMock,addContractMock} from '@chainlink/cre-sdk/test';
import {bigintToProtoBigInt,hexToBase64} from '@chainlink/cre-sdk';
import {testnets} from '../lending/testnets.js';
import {readAbi} from '../lending/observation.js';
import {onTrigger} from '../cre-lending/workflow/workflow.js';
import {evaluateRun} from '../lending/run.js';
import {FIXTURE_NOW} from '../lending/fixtures.js';
// @ts-ignore test fixture intentionally imports the built shared modules.
import {payloadFixture} from './helpers/lending-payload.mjs';
function setup(days:30|60=60,change?:'hash'|'rate'|'quote'|'finality') {
 const payload=payloadFixture(days);
 const runtime=newTestRuntime(null,{timeProvider:()=>Number(FIXTURE_NOW)*1000},{mode:'testnet-read-only' as const,configVersion:'part2-testnets-v1' as const});
 testnets.forEach((n,i)=>{
  const c=payload.chains[i],mock=EvmMock.testInstance(BigInt(n.selector));
  mock.headerByNumber=()=>({header:{timestamp:FIXTURE_NOW,blockNumber:bigintToProtoBigInt(change==='finality'?9:10),
   hash:hexToBase64(change==='hash'?'0x'+'cd'.repeat(32):c.blockHash),parentHash:hexToBase64('0x'+'00'.repeat(32))}});
  const wallet=addContractMock(mock,{address:c.wallet,abi:readAbi});
  wallet.observe=()=>c.values.map((v:string,j:number)=>BigInt(change==='rate'&&j===3?'0':v));
  wallet.binding=()=>c.binding;wallet.quoteLink=()=>BigInt(change==='quote'?'0':c.quoteLinkUnits);
  addContractMock(mock,{address:n.token,abi:readAbi}).decimals=()=>18;
  addContractMock(mock,{address:n.link,abi:readAbi}).decimals=()=>18;
  mock.writeReport=()=>{throw Error('A dry run attempted to write');};
 });
 return {runtime,payload,trigger:{input:new TextEncoder().encode(JSON.stringify(payload))} as any};
}
for(const [days,action] of [[60,'move'],[30,'hold']] as const)test(`SDK handler ${action} matches same immutable input and shared clock`,()=>{
 const {runtime,payload,trigger}=setup(days);
 const actual=JSON.parse(onTrigger(runtime,trigger));
 expect(actual.result).toEqual(evaluateRun(payload.run,()=>FIXTURE_NOW));
 expect(actual.result.decision.action).toBe(action);
 for(const line of runtime.getLogs()) expect(new TextEncoder().encode(line).length).toBeLessThanOrEqual(1024);
 expect(actual.result.context.policyVersion).toBe(payload.run.policy.version);
});
for(const change of ['hash','rate','quote','finality'] as const)test(`SDK handler rejects changed ${change}`,()=>{
 const {runtime,trigger}=setup(60,change);expect(()=>onTrigger(runtime,trigger)).toThrow();
});
test('SDK handler bounds untrusted payload before JSON validation',()=>{
 const {runtime}=setup();expect(()=>onTrigger(runtime,{input:new Uint8Array(65537)} as any)).toThrow('Oversized');
});


// The arithmetic entry point is intentionally separate from deployed-contract reads.
import {onTrigger as onArithmeticTrigger} from '../cre-lending/arithmetic-check/workflow.js';
import {exampleRun} from '../lending/fixtures.js';
for(const [days,action] of [[60,'move'],[30,'hold']] as const)test(`arithmetic CRE entry labels ${action} as fixture-only`,()=>{
 const runtime=newTestRuntime(null,{timeProvider:()=>Number(FIXTURE_NOW)*1000},{mode:'local-simulation' as const,scope:'arithmetic-only' as const});
 const input=exampleRun(days);
 const actual=JSON.parse(onArithmeticTrigger(runtime,{input:new TextEncoder().encode(JSON.stringify(input))} as any));
 expect(actual.deployedContractsRead).toBe(false);expect(actual.broadcast).toBe(false);
 expect(actual.result).toEqual(evaluateRun(input,()=>FIXTURE_NOW));expect(actual.result.decision.action).toBe(action);
});
test('arithmetic CRE entry rejects observations presented as testnet evidence',()=>{
 const runtime=newTestRuntime(null,{timeProvider:()=>Number(FIXTURE_NOW)*1000},{mode:'local-simulation' as const,scope:'arithmetic-only' as const});
 const input=JSON.parse(JSON.stringify(exampleRun(60)));input.snapshot.position.evidence.source='testnet:11155111';
 expect(()=>onArithmeticTrigger(runtime,{input:new TextEncoder().encode(JSON.stringify(input))} as any)).toThrow('labelled arithmetic fixtures');
});
