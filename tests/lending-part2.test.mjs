import {test} from 'node:test';
import assert from 'node:assert/strict';
import {decodeAbiParameters,encodeFunctionData,parseAbi,hashTypedData} from 'viem';
import {readFileSync} from 'node:fs';
import {rayToBps,payloadSchema,validateObservedInput} from '../.local/lending-build/observation.js';
import {encodeInstructionReport,instructionParameters} from '../.local/lending-build/report.js';
import {evaluateRun} from '../.local/lending-build/run.js';
import {FIXTURE_NOW} from '../.local/lending-build/fixtures.js';
import {payloadFixture} from './helpers/lending-payload.mjs';
test('adapter reuses shared evaluator for both move and hold, preserving immutable run',()=>{
 for(const [days,action] of [[60,'move'],[30,'hold']]){
  const p=payloadSchema.parse(payloadFixture(days)),input=validateObservedInput(p);
  assert.equal(evaluateRun(input,()=>FIXTURE_NOW).decision.action,action);
  assert.equal(input.runId,p.run.runId);assert.ok(Object.isFrozen(input));
 }
});
test('RAY normalization is integer exact and refuses fractional basis points',()=>{
 assert.equal(rayToBps(4n*10n**25n),'400');assert.equal(rayToBps(7n*10n**25n),'700');
 for(const n of [-1n,1n,400n*10n**23n+1n,1000001n*10n**23n])assert.throws(()=>rayToBps(n));
});
test('observed amount, reserve, rates, original LINK quote and fee funding cannot drift',()=>{
 for(const mutate of [p=>p.chains[0].values[0]='1',p=>p.chains[0].values[4]='1',p=>p.chains[0].values[5]='1',
  p=>p.chains[0].values[3]='1',p=>p.chains[1].values[7]='1',p=>p.chains[1].values[4]='1',
  p=>p.chains[0].values[8]='1',p=>p.chains[0].values[9]='1',p=>p.chains[0].quoteLinkUnits='1',
  p=>p.chains[0].values[11]='1',p=>p.chains[0].blockNumber='1',p=>p.chains[0].pool=p.chains[1].pool,
  p=>p.run.policy.maxMovableUnits='1',p=>p.chains[1].wallet=p.chains[0].wallet,
  p=>p.run.snapshot.currentPool.evidence.observedAt='1']){
  const p=payloadFixture();mutate(p);assert.throws(()=>validateObservedInput(payloadSchema.parse(p)));
 }
});
test('mainnet, duplicate chains, unknown payload fields and wrong decimals fail closed',()=>{
 for(const mutate of [p=>p.chains[0].chainId='1',p=>p.chains[1]=p.chains[0],p=>p.broadcast=true,p=>p.chains[0].tokenDecimals=6]){
  const p=payloadFixture();mutate(p);assert.throws(()=>validateObservedInput(payloadSchema.parse(p)));
 }
});
test('viem raw report matches cross-language golden vector, function calldata differs',()=>{
 const v=JSON.parse(readFileSync(new URL('./helpers/report-vector.json',import.meta.url)));
 const i={...v.instruction,sourceChainId:BigInt(v.instruction.sourceChainId),destinationSelector:BigInt(v.instruction.destinationSelector),amount:BigInt(v.instruction.amount),expiry:BigInt(v.instruction.expiry)};
 const raw=encodeInstructionReport(i,v.signature);assert.equal(raw,v.raw);
 assert.equal(hashTypedData({domain:{name:'LendingTestInstruction',version:'1',chainId:11155111,verifyingContract:i.consumer},
  types:{Instruction:[{name:'operationId',type:'bytes32'},{name:'sourceChainId',type:'uint256'},{name:'consumer',type:'address'},
   {name:'destinationSelector',type:'uint64'},{name:'destination',type:'address'},{name:'token',type:'address'},{name:'amount',type:'uint256'},
   {name:'policyVersion',type:'bytes32'},{name:'expiry',type:'uint256'}]},primaryType:'Instruction',message:i}),v.eip712Digest);
 assert.deepEqual(decodeAbiParameters(instructionParameters,raw),[i,v.signature]);
 const calldata=encodeFunctionData({abi:parseAbi(['function admit((bytes32 operationId,uint256 sourceChainId,address consumer,uint64 destinationSelector,address destination,address token,uint256 amount,bytes32 policyVersion,uint256 expiry),bytes)']),functionName:'admit',args:[i,v.signature]});
 assert.notEqual(calldata,raw);assert.equal(calldata.slice(10),raw.slice(2));
});

test('public setup artifacts preserve exact funding amounts and isolate destination principal',async()=>{
 const {mkdtempSync,writeFileSync,readFileSync,rmSync}=await import('node:fs');
 const {tmpdir}=await import('node:os');const {join}=await import('node:path');const {spawnSync}=await import('node:child_process');
 const dir=mkdtempSync(join(tmpdir(),'lending-public-plan-test-'));
 try{
  const p=JSON.parse(readFileSync(new URL('../examples/lending-testnet-plan.json',import.meta.url)));
  Object.assign(p,{owner:'0x'+'11'.repeat(20),workflowId:'0x'+'ab'.repeat(32),approvedAt:FIXTURE_NOW,
   deadline:String(BigInt(FIXTURE_NOW)+60n*86400n),principalUnits:'8000000000000000000',protectedIdleReserveUnits:'2000000000000000000',
   maxMovableUnits:'8000000000000000000',poolCapacityUnits:'1000000000000000000000',estimatedSourceGasWei:'1000000000000',
   estimatedDestinationApprovalGasWei:'1000000000000',exitIfStayMicrounits:'100',exitIfMoveMicrounits:'100',
   sourceLinkFundingUnits:'1000000000000000000',destinationLinkFundingUnits:'1000000000000000000'});
  const plan=join(dir,'plan.json'),out=join(dir,'review');writeFileSync(plan,JSON.stringify(p));
  let child=spawnSync(process.execPath,['scripts/lending-plan.mjs',plan,out],{encoding:'utf8'});assert.equal(child.status,0,child.stderr);
  const review=JSON.parse(readFileSync(join(out,'deployment-review.json')));
  assert.equal(review.deployments[0].principalUnits,p.principalUnits);assert.equal(review.deployments[1].principalUnits,'0');
  const seed=JSON.parse(readFileSync(join(out,'seed-run.json')));
  assert.equal(seed.policy.minNetBenefitMicrounits,'1000000');assert.equal(seed.policy.maxFeeMicrounits,'3000000');
  assert.equal(seed.snapshot.costs.components.find(c=>c.category==='sourceGas').amountUnits,p.estimatedSourceGasWei);
  const manifest=JSON.parse(readFileSync(join(out,'manifest.json')));
  manifest.chains.forEach((c,i)=>{c.pool='0x'+String(i+4).repeat(40);c.wallet='0x'+String(i+2).repeat(40);});
  writeFileSync(join(out,'manifest.json'),JSON.stringify(manifest));
  child=spawnSync(process.execPath,['scripts/lending-funding-artifacts.mjs',join(out,'manifest.json'),join(out,'deployment-review.json'),join(out,'funding.json')],{encoding:'utf8'});
  assert.equal(child.status,0,child.stderr);
  const funding=JSON.parse(readFileSync(join(out,'funding.json')));
  assert.equal(funding.chains[0].transactions.find(t=>t.functionName==='transfer').args[1],'10000000000000000000');
  assert.equal(funding.chains[0].transactions.find(t=>t.functionName==='depositToPool').args[0],p.principalUnits);
  assert.ok(!funding.chains[1].transactions.some(t=>t.functionName==='depositToPool'));
  assert.equal(funding.chains[1].expectedIdleUnits,p.protectedIdleReserveUnits);
  child=spawnSync(process.execPath,['scripts/lending-plan.mjs',plan,out],{encoding:'utf8'});assert.notEqual(child.status,0,'Must not overwrite review');
  manifest.chains[0].chainId='1';writeFileSync(join(out,'manifest.json'),JSON.stringify(manifest));
  child=spawnSync(process.execPath,['scripts/lending-funding-artifacts.mjs',join(out,'manifest.json'),join(out,'deployment-review.json'),join(out,'mainnet.json')],{encoding:'utf8'});
  assert.notEqual(child.status,0,'Must reject mainnet artifacts');
 }finally{rmSync(dir,{recursive:true,force:true});}
});
test('unfilled example plan fails before creating transaction artifacts',async()=>{
 const {spawnSync}=await import('node:child_process');
 const child=spawnSync(process.execPath,['scripts/lending-plan.mjs','examples/lending-testnet-plan.json','/tmp/lending-must-not-create'],{encoding:'utf8'});
 assert.notEqual(child.status,0);assert.match(child.stderr,/Invalid input|expected string/i);
});


test('CRE evidence logging preserves Unicode below the per-line quota and rejects truncation',async()=>{
 const {resultLogLines,readLoggedResult}=await import('../.local/lending-build/cre-evidence.js');
 const value={userId:'test-😀-भारत',result:'x'.repeat(850)+' '.repeat(900)+'y'.repeat(3500)};
 const lines=resultLogLines(JSON.stringify(value));
 assert.ok(lines.length>1);for(const line of lines)assert.ok(new TextEncoder().encode(line).length<=1024);
 assert.deepEqual(readLoggedResult(lines.map(line=>'[USER LOG] '+line).join('\n')),value);
 assert.throws(()=>readLoggedResult(lines.slice(1).join('\n')));
 assert.throws(()=>readLoggedResult(lines.join('\n')+'\nPART2_RESULT_CHUNK=0/99:changed'));
});
