// Fixed JSON boundary. No shell, signing, user-selected paths, RPCs or destinations.
import {exampleRun} from '../../.local/lending-build/fixtures.js';
import {createRunInput, policySchema} from '../../.local/lending-build/schema.js';
import {evaluateRun} from '../../.local/lending-build/run.js';
import {networkConfig} from '../../.local/lending-build/networks.js';
import {readLoggedResult} from '../../.local/lending-build/cre-evidence.js';
import {spawn} from 'node:child_process';
import {isDeepStrictEqual} from 'node:util';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir,homedir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {existsSync} from 'node:fs';
import {executionFailure} from './cre-execution.mjs';

export async function native(input,observation=null) {
 if(observation&&!isDeepStrictEqual(input,observation.run))throw Error('Observation and evaluation input differ');
 const dir=await mkdtemp(resolve(tmpdir(),'lending-evaluation-'));
 try {
  const path=resolve(dir,'input.json');await writeFile(path,JSON.stringify(observation??input),{mode:0o600});
  const cli=existsSync(resolve(homedir(),'.cre/bin/cre'))?resolve(homedir(),'.cre/bin/cre'):'cre';
  const args=['workflow','simulate',observation?'workflow':'arithmetic-check','--target',observation?'testnet-fast-read-only':'local-simulation','--non-interactive','--trigger-index','0','--http-payload',path];
  const output=await new Promise((ok,fail)=>{
   const child=spawn(cli,args,{cwd:resolve('cre-lending'),stdio:['ignore','pipe','pipe'],
    env:{...process.env,PATH:resolve('node_modules/.bin')+':'+process.env.PATH}});
   let data='',done=false;
   const finish=(err,value)=>{if(done)return;done=true;clearTimeout(timer);err?fail(err):ok(value);};
   const timer=setTimeout(()=>{child.kill('SIGKILL');finish(Error('CRE evaluation timed out; no transfer admitted'));},120000);
   const append=b=>{data+=b;if(data.length>2000000){child.kill('SIGKILL');finish(Error('CRE output too large'));}};
   child.stdout.on('data',append);child.stderr.on('data',append);
   child.on('error',()=>finish(Error('CRE CLI unavailable; no transfer admitted')));
   child.on('close',code=>{
    const failure=code===0?null:executionFailure(data);
    finish(failure?Object.assign(Error(failure.message),{failureCode:failure.code}):null,data);
   });
  });
  const result=readLoggedResult(output);
  if(observation){
   if(result.mode!=='testnet-mined-read-only'||!isDeepStrictEqual(result.chains,observation.chains))throw Error('Unexpected CRE observation evidence');
  }else if(result.scope!=='arithmetic-only'||result.broadcast!==false||result.deployedContractsRead!==false)
   throw Error('Unexpected CRE evidence scope');
  // A binding-level rejection has no evaluator clock and cannot authorize a move.
  if(result.result.decision.evaluatedAt===null) {
   if(result.result.decision.action!=='blocked')throw Error('Missing CRE clock');
   return {input,result:result.result,engine:'native-local-CRE',broadcast:false};
  }
  const expected=evaluateRun(input,()=>result.result.decision.evaluatedAt);
  if(!isDeepStrictEqual(result.result,expected))throw Error('CRE and application decisions disagree');
  return {input,result:result.result,engine:'native-local-CRE',broadcast:false};
 } finally {await rm(dir,{recursive:true,force:true});}
}
export function planDurationSeconds(settings) {
 const {days,durationSeconds}=settings;
 const hasDays=days!=null,hasSeconds=durationSeconds!=null;
 if(hasDays&&(!Number.isSafeInteger(days)||days<1||days>365))throw Error('Invalid plan days');
 if(hasSeconds&&(!Number.isSafeInteger(durationSeconds)||durationSeconds<60||durationSeconds>365*86400))throw Error('Invalid plan duration');
 if(hasDays&&hasSeconds&&days*86400!==durationSeconds)throw Error('Conflicting plan durations');
 return BigInt(hasSeconds?durationSeconds:(hasDays?days:30)*86400);
}
async function main(){
let raw='';for await(const chunk of process.stdin){raw+=chunk;if(raw.length>131072)throw Error('Oversized request');}
try {
 const p=JSON.parse(raw);
 if(p.action==='plan') {
  const run=structuredClone(exampleRun(30)),s=p.settings,now=p.now;
  run.policy={...run.policy,version:p.version,approvalId:'pending:'+p.planId,userId:p.user,
   walletAddress:p.wallet,approvedAt:now,deadline:String(BigInt(now)+planDurationSeconds(s)),
   protectedIdleReserveUnits:s.reserveUnits,maxMovableUnits:s.amountUnits,
   minNetBenefitMicrounits:s.minNetBenefitMicrounits,maxFeeMicrounits:s.maxFeeMicrounits,
   maxObservationAgeSeconds:s.maxObservationAgeSeconds,maxObservationSkewSeconds:s.maxObservationSkewSeconds,
   cooldownSeconds:s.cooldownSeconds};
  console.log(JSON.stringify({policy:policySchema.parse(run.policy),approvedPools:networkConfig.networks}));
 } else if(p.action==='evaluate') {
  const r=structuredClone(exampleRun(30)),l=p.ledger,policy=policySchema.parse(p.policy);
  const source=networkConfig.networks.find(n=>n.chainId===l.chainId),dest=networkConfig.networks.find(n=>n.chainId!==l.chainId);
  if(!source||!dest)throw Error('Only approved testnets are permitted');
  const loc=n=>({chainId:n.chainId,poolId:n.poolId,tokenId:n.tokenId});
  r.runId=p.runId;r.operationId=p.operationId;r.policy=policy;
  r.context={userId:policy.userId,walletAddress:policy.walletAddress,walletBindingId:p.binding,policyVersion:policy.version};
  r.observation={snapshotId:p.runId,assembledAt:p.now,reference:p.runId};
  const s=r.snapshot;s.id=p.runId;s.position={...s.position,id:p.binding,userId:policy.userId,
   walletAddress:policy.walletAddress,location:loc(source),investedUnits:l.principalUnits,movableUnits:l.principalUnits};
  // Preserve source-chain reserve separately when the managed position moves.
  s.reserve={...s.reserve,chainId:l.chainId,idleBalanceUnits:l.idle[l.chainId]};
  s.currentPool={...s.currentPool,location:loc(source),aprBps:l.rates[l.chainId],withdrawalAvailableUnits:l.principalUnits};
  s.destinationPool={...s.destinationPool,location:loc(dest),aprBps:l.rates[dest.chainId],depositCapacityUnits:l.capacityUnits};
  for(const c of s.costs.components){c.chainId=['destinationGas','destinationApprovalGas','depositFee'].includes(c.category)?dest.chainId:source.chainId;
   c.amountUnits=c.category==='bridgeFee'?l.moveFeeUnits:'0';}
  s.feeBalances=networkConfig.networks.map(n=>({...s.feeBalances[0],chainId:n.chainId,availableUnits:l.fees[n.chainId]}));
  s.pendingOperation=null;s.accounting=l.accounting;
  // This backend is explicitly a local ledger, not an onchain observation adapter.
  const rebase=v=>{if(!v||typeof v!=='object')return;for(const [k,x]of Object.entries(v)){
   if(k==='observedAt')v[k]=p.now;else rebase(x);}};rebase(s);
  const input=createRunInput(r);
  console.log(JSON.stringify(p.native?await native(input):{input,result:evaluateRun(input,()=>p.now),engine:'shared-rules-test-harness',broadcast:false}));
 } else throw Error('Unknown rules action');
} catch(e) {console.log(JSON.stringify({error:e.message}));process.exitCode=1;}

}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)await main();
