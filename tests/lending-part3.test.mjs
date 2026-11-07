import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationJournal} from '../scripts/part3/journal.mjs';
test('a proven CLI initialization rejection can close the claim; unknown outcomes cannot',t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);
 for(const execution of [{}, {notSubmitted:true,exitCode:1,error:{code:'CRE_TIMEOUT'}},
  {notSubmitted:false,exitCode:1,error:{code:'CRE_SIGNER_UNAVAILABLE'}}])
  assert.throws(()=>j.cancelBeforeBroadcast(id,execution),/Missing proof/);
 assert.equal(j.get(id).state,'submission_unknown');
 assert.equal(j.cancelBeforeBroadcast(id,{notSubmitted:true,exitCode:1,
  error:{code:'CRE_SIGNER_UNAVAILABLE',message:'CLI stopped before execution'}}).state,'cancelled');
 assert.equal(j.get(id).details.broadcastAttempted,false);
});
import {reconcile,verifySourceReceipt,verifyDestination,recoveryArtifact,verifyMessage,getMessage,resolveNoSend,settleArtifact} from '../scripts/part3/monitor.mjs';
import {executionFixture,sourceReceipt,destinationReceipt,messageFixture,H,M,D} from './helpers/lending-part3.mjs';
import {validateExecutionPayload} from '../.local/lending-build/part3/report.js';
import {verifyRuntimeCode} from '../scripts/part3/code.mjs';
import {spawn} from 'node:child_process';
import {writeFileSync,readFileSync} from 'node:fs';
import {once} from 'node:events';
import {hashTypedData} from 'viem';
import {signingRequest,encodeExecutionReport} from '../.local/lending-build/part3/report.js';
function store(t){const dir=mkdtempSync(join(tmpdir(),'lending-part3-'));const path=join(dir,'journal.sqlite');
 t.after(()=>rmSync(dir,{recursive:true,force:true}));const j=new OperationJournal(path);t.after(()=>{try{j.close();}catch{}});return {j,path};}
function claimed(j,p){j.prepare(p);j.claim(p.instruction.operationId);return p.instruction.operationId;}
function clients(p,receipt=destinationReceipt(p)){
 return id=>({getTransactionReceipt:async()=>id==='11155111'?sourceReceipt(p):receipt,
  getBlock:async ({blockNumber})=>({number:100n,timestamp:blockNumber===20n?1000n:2000n,hash:blockNumber===20n?H:D}),getLogs:async()=>[],readContract:async()=>BigInt(p.instruction.amount)});
}
test('duplicate operation and second candidate for same owner are atomically blocked',t=>{
 const {j,path}=store(t),p=executionFixture();j.prepare(p);const second=new OperationJournal(path);t.after(()=>second.close());
 assert.throws(()=>second.prepare(p),/already exists/);
 const other=structuredClone(p);other.instruction.operationId=H;assert.throws(()=>second.prepare(other),/UNIQUE/);
 j.claim(p.instruction.operationId);assert.throws(()=>second.claim(p.instruction.operationId),/already started/);
});
test('claim is durable before submission; restart with no hash never retries or unlocks',async t=>{
 const {j,path}=store(t),p=executionFixture(),id=claimed(j,p);j.close();const restarted=new OperationJournal(path);t.after(()=>restarted.close());
 const result=await reconcile(restarted,id,clients(p));assert.equal(result.state,'submission_unknown');assert.match(result.details.failureReason,/do not resend/);
 assert.throws(()=>restarted.claim(id),/already started/);assert.throws(()=>restarted.update(id,'cancelled'),/Unsafe/);
});
test('uncertain hashless submission reconciles an actual source event',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);const c=clients(p);
 const result=await reconcile(j,id,chain=>({...c(chain),getLogs:async()=>[{transactionHash:H}]}),async()=>messageFixture(p,'VERIFYING'));
 assert.equal(result.state,'in_transit');assert.equal(result.details.messageId,M);assert.equal(result.details.sourceHash,H);
});
test('restart during transit keeps balances uncredited until deposit proof',async t=>{
 const {j,path}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 await reconcile(j,id,clients(p),async()=>messageFixture(p,'VERIFYING'));j.close();
 const restarted=new OperationJournal(path);t.after(()=>restarted.close());assert.equal(restarted.get(id).state,'in_transit');
 assert.equal(restarted.get(id).details.verifiedDestinationPrincipal,null);
 const result=await reconcile(restarted,id,clients(p),async()=>messageFixture(p));assert.equal(result.state,'verified_deposit');assert.equal(result.details.verifiedDestinationPrincipal,p.instruction.amount);
 const settlement=settleArtifact(result);assert.equal(settlement.to,p.instruction.consumer);assert.equal(settlement.kind,'settle-verified-operation');
});
test('outer receipt success without inner consumer event is failed and remains locked',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});const c=clients(p);
 const result=await reconcile(j,id,chain=>({...c(chain),getTransactionReceipt:async()=>sourceReceipt(p,{logs:[]})}));
 assert.equal(result.state,'failed');assert.match(result.details.failureReason,/inner report consumer failed/);assert.ok(j.pending(result.wallet));
});
test('API timeout or unindexed message is pending, not a failed transfer',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 const result=await reconcile(j,id,clients(p),async()=>null);assert.equal(result.state,'in_transit');assert.match(result.details.failureReason,/not failure/);
 assert.equal(await getMessage(M,async()=>({status:404})),null);
});
test('delivery success with caught pool failure tracks idle tokens and exact deposit recovery',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 let r=await reconcile(j,id,clients(p,destinationReceipt(p,true)),async()=>messageFixture(p));
 assert.equal(r.state,'idle_tokens');assert.equal(r.details.verifiedDestinationPrincipal,null);assert.equal(r.details.idleTokenUnits,p.instruction.amount);
 const artifact=await recoveryArtifact(r,clients(p));assert.equal(artifact.to,p.instruction.destination);assert.equal(artifact.messageId,M);assert.equal(artifact.kind,'retry-existing-deposit');
 j.update(id,'idle_tokens',{recoveryHash:D});r=await reconcile(j,id,clients(p),async()=>messageFixture(p));assert.equal(r.state,'verified_deposit');
 assert.equal(r.details.sourceHash,H);assert.equal(r.details.messageId,M);
});
test('CCIP failure only offers recovery after current eligibility; never prepares another send',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 const failed={...messageFixture(p,'FAILED'),readyForManualExecution:false};let r=await reconcile(j,id,clients(p),async()=>failed);
 assert.equal(r.state,'destination_failed');await assert.rejects(()=>recoveryArtifact(r,clients(p),async()=>failed),/not currently eligible/);
 const artifact=await recoveryArtifact(r,clients(p),async()=>({...failed,readyForManualExecution:true}));assert.equal(artifact.messageId,M);assert.equal(artifact.kind,'existing-message-recovery');
});
test('network verification wait is distinct from an application execution failure',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 const message={...messageFixture(p,'SENT'),finalityType:'FINALIZED',executor:{status:'AWAITING_VERIFICATION'}};
 const r=await reconcile(j,id,clients(p),async()=>message,{confirmation:'mined'});
 assert.equal(r.state,'in_transit');assert.equal(r.details.failureReason,null);
 assert.equal(r.details.ccipFinalityType,'FINALIZED');assert.equal(r.details.ccipExecutorStatus,'AWAITING_VERIFICATION');
});
test('wrong destination principal/token/receipt cannot count as verified',async()=>{
 const p=executionFixture(),r={immutable:p,details:{messageId:M,sourceHash:H}};
 await assert.rejects(()=>verifyDestination(r,destinationReceipt(p),{readContract:async()=>0n}),/position increase/);
 const missing=destinationReceipt(p);missing.logs.pop();await assert.rejects(()=>verifyDestination(r,missing,{readContract:async()=>BigInt(p.instruction.amount)}),/transfer into/);
 const wrong=messageFixture(p);wrong.receiver=p.instruction.consumer;assert.throws(()=>verifyMessage(r,wrong),/identity/);
 assert.throws(()=>verifySourceReceipt(r,sourceReceipt(p,{status:'reverted'})),/reverted/);
});
test('unfinalized and reorged receipts cannot complete',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 const c=clients(p);const result=await reconcile(j,id,chain=>({...c(chain),getBlock:async()=>({number:1n,hash:H})}));assert.equal(result.state,'submitted');
 await assert.rejects(()=>reconcile(j,id,chain=>({...c(chain),getBlock:async()=>({number:100n,hash:M})})),/Noncanonical/);
});
test('testnet mined confirmation completes from matching receipts without requesting finality',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 const c=clients(p),mined=id=>({...c(id),getBlock:async args=>{
  assert.notEqual(args.blockTag,'finalized');assert.notEqual(args.blockTag,'safe');return c(id).getBlock(args);
 }});
 const result=await reconcile(j,id,mined,async()=>messageFixture(p),{confirmation:'mined'});
 assert.equal(result.state,'verified_deposit');assert.equal(result.details.confirmation,'mined');
});
test('testnet no-send recovery checks mined expiry and nonces without a finalized checkpoint',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p),zero='0x'+'00'.repeat(32);
 const c={getBlock:async args=>{assert.notEqual(args.blockTag,'finalized');return {number:30n,hash:H,timestamp:BigInt(p.instruction.expiry)};},
  readContract:async()=>zero,getTransactionCount:async()=>7};
 const result=await resolveNoSend(j,id,c,{confirmation:'mined'});
 assert.equal(result.state,'cancelled');assert.equal(result.details.noSendProof.confirmation,'mined');
 assert.ok(!result.details.noSendProof.finalizedTimestamp);
});
test('signed input binding and bundled fee accounting fail closed',()=>{
 const p=executionFixture();assert.deepEqual(validateExecutionPayload(p),p);const altered=structuredClone(p);altered.observation.run.policy.maxFeeMicrounits='999999999';
 assert.throws(()=>validateExecutionPayload(altered),/differs/);
});
test('runtime bytecode check permits only declared immutable slots to vary',()=>{
 const artifact={deployedBytecode:{object:'0x1122334455',immutableReferences:{'1':[{start:1,length:2}]}}};
 assert.equal(verifyRuntimeCode('0x11abcd4455',artifact),true);
 assert.throws(()=>verifyRuntimeCode('0x11abcd4456',artifact),/reviewed Part 3/);
 assert.throws(()=>verifyRuntimeCode('0x1122',artifact),/length/);
});
test('the full Part 3 EIP-712 instruction and raw report match the cross-language vector',()=>{
 const vector=JSON.parse(readFileSync(new URL('./helpers/part3-report-vector.json',import.meta.url),'utf8'));
 assert.equal(hashTypedData(signingRequest(vector)),vector.digest);assert.equal(encodeExecutionReport(vector),vector.raw);
});
test('an unknown source attempt only unlocks after finalized expiry, zero message state and reconciled nonces',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p),zero='0x'+'00'.repeat(32);
 const c={getBlock:async()=>({number:30n,hash:H,timestamp:BigInt(p.instruction.expiry)}),readContract:async()=>zero,getTransactionCount:async()=>7};
 await assert.rejects(()=>resolveNoSend(j,id,{...c,getBlock:async()=>({number:30n,hash:H,timestamp:0n})}),/still executable/);
 await assert.rejects(()=>resolveNoSend(j,id,{...c,readContract:async()=>M}),/source operation exists/);
 await assert.rejects(()=>resolveNoSend(j,id,{...c,getTransactionCount:async({blockTag})=>blockTag==='pending'?8:7}),/pending transaction/);
 const result=await resolveNoSend(j,id,c);assert.equal(result.state,'cancelled');assert.equal(result.details.recoveryStatus,'verified-no-send');assert.equal(j.pending(result.wallet),null);
 assert.throws(()=>j.prepare(p),/already exists/);
});
test('separate processes cannot prepare two operations for one wallet',async t=>{
 const dir=mkdtempSync(join(tmpdir(),'lending-concurrent-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));
 const p=executionFixture(),other=structuredClone(p);other.instruction.operationId=H;
 writeFileSync(join(dir,'first.json'),JSON.stringify(p));writeFileSync(join(dir,'second.json'),JSON.stringify(other));
 const code=`import {readFileSync} from 'node:fs';import {OperationJournal} from ${JSON.stringify(new URL('../scripts/part3/journal.mjs',import.meta.url).href)};
  try{const j=new OperationJournal(process.argv[1]);j.prepare(JSON.parse(readFileSync(process.argv[2],'utf8')));j.close();}catch{process.exitCode=1;}`;
 const statuses=await Promise.all(['first','second'].map(async name=>{const child=spawn(process.execPath,['--input-type=module','-e',code,join(dir,'journal.sqlite'),join(dir,name+'.json')],{stdio:'ignore'});return (await once(child,'exit'))[0];}));
 assert.deepEqual(statuses.sort(),[0,1]);
});
test('lost deposit-retry hash is discovered from finalized destination events after restart',async t=>{
 const {j}=store(t),p=executionFixture(),id=claimed(j,p);j.update(id,'submitted',{sourceHash:H});
 await reconcile(j,id,clients(p,destinationReceipt(p,true)),async()=>messageFixture(p));
 const c=clients(p);const result=await reconcile(j,id,chain=>({...c(chain),getLogs:async request=>request.event.name==='PoolDeposited'?[{transactionHash:D}]:[]}),async()=>messageFixture(p));
 assert.equal(result.state,'verified_deposit');assert.equal(result.details.recoveryHash,D);assert.equal(result.details.messageId,M);assert.equal(result.details.actualTransitSeconds,'1000');
});
test('signing helper serves only public review artifacts and rejects unexpected origins of requests',async t=>{
 const dir=mkdtempSync(join(tmpdir(),'lending-sign-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));
 const p=executionFixture();delete p.signature;writeFileSync(join(dir,'unsigned.json'),JSON.stringify(p));
 const child=spawn(process.execPath,['scripts/part3/sign-server.mjs',dir],{stdio:['ignore','pipe','pipe']});t.after(()=>child.kill());
 const url=await new Promise((resolveUrl,reject)=>{child.once('error',reject);child.stdout.on('data',data=>{const match=data.toString().match(/http:\/\/127\.0\.0\.1:\d+\/[a-f0-9]+\//);if(match)resolveUrl(match[0]);});child.once('exit',code=>reject(Error('Sign helper exited '+code)));});
 const data=await fetch(url+'payload').then(r=>r.json());assert.equal(data.unsigned.instruction.operationId,p.instruction.operationId);
 assert.equal((await fetch(url+'payload',{method:'POST'})).status,403);
 assert.equal((await fetch(new URL('/payload',url))).status,403);
 assert.equal((await fetch(url+'sign.js')).status,200);
 assert.equal((await fetch(url+'state.json')).status,404);
});
