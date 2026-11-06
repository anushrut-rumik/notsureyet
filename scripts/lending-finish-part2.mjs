// Continues read-only verification after user-signed setup. Never signs/sends.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {verifyTypedData} from 'viem';
import {approvalData,clientFor} from './lib/lending-setup.mjs';
import {observations} from '../.local/lending-build/schema.js';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const [phase,directory='.local/lending-metamask']=process.argv.slice(2);
if(!['move','hold'].includes(phase))throw Error('Usage: lending-finish-part2.mjs move|hold [state-directory]');
const dir=resolve(root,directory),statePath=resolve(dir,'state.json');
const load=async p=>JSON.parse(await readFile(p,'utf8'));
const save=(p,value)=>writeFile(p,JSON.stringify(value,null,2)+'\n',{flag:'wx'});
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function command(script,args){return new Promise((resolveCommand,reject)=>{
 const p=spawn(process.execPath,[resolve(root,script),...args],{cwd:root,stdio:'inherit'});
 p.on('error',reject);p.on('exit',code=>code===0?resolveCommand():reject(Error(script+' exited '+code)));
});}
async function waitSetup(){
 let previous='';
 for(;;){const s=await load(statePath);const required=s.plan.actions.filter(a=>phase==='hold'||a.phase!=='hold');
  const done=required.filter(a=>s.records[a.id]?.status==='confirmed').length;
  const message=`${phase}: ${done}/${required.length} user-signed setup receipts confirmed`;
  if(message!==previous){console.log(message);previous=message;}
  if(required.some(a=>s.records[a.id]?.status==='failed'))throw Error('A setup transaction failed. Reconcile it before verification.');
  if(done===required.length)return s;
  await delay(10000);
 }
}
async function waitFinality(s){
 let previous='';
 for(;;){const progress=await Promise.all(s.plan.chains.map(async n=>{
   const c=clientFor(n.chainId);if(String(await c.getChainId())!==n.chainId)throw Error('Wrong RPC chain');
   const actions=s.plan.actions.filter(a=>a.chainId===n.chainId&&(phase==='hold'||a.phase!=='hold'));
   const target=actions.reduce((m,a)=>BigInt(s.records[a.id].blockNumber)>m?BigInt(s.records[a.id].blockNumber):m,0n);
   const b=await c.getBlock({blockTag:'finalized'});return {chainId:n.chainId,finalized:String(b.number),required:String(target),ready:b.number>=target};
  }));
  if(progress.every(p=>p.ready))return;
  const message=JSON.stringify(progress);if(message!==previous){console.log('Waiting for finalized setup receipts: '+message);previous=message;}
  await delay(20000);
 }
}
try{
 const s=await waitSetup();
 if(!s.approval||!await verifyTypedData({...approvalData(s.plan),address:s.plan.owner,signature:s.approval.signature}))throw Error('Missing exact owner-signed setup policy');
 await waitFinality(s);
 const now=String(Math.floor(Date.now()/1000)),p=s.plan;
 const runDir=resolve(dir,phase+'-run-'+now);await mkdir(runDir);
 const rpcGasPrices=await Promise.all(p.chains.map(n=>clientFor(n.chainId).getGasPrice()));
 const finalizedBlocks=await Promise.all(p.chains.map(n=>clientFor(n.chainId).getBlock({blockTag:'finalized'})));
 const gasPrices=p.chains.map(n=>Object.entries(s.records).filter(([id,r])=>id.startsWith(n.chainId)&&r.effectiveGasPrice).reduce((max,[,r])=>BigInt(r.effectiveGasPrice)>max?BigInt(r.effectiveGasPrice):max,0n));
 const scenarioTime=String(finalizedBlocks.reduce((min,b)=>b.timestamp<min?b.timestamp:min,finalizedBlocks[0].timestamp));
 const [sourceGasPrice,destinationGasPrice]=gasPrices;
 const completed={scope:p.scope,owner:p.owner,userId:'test-'+p.owner.slice(2,10),runId:'p2-'+phase+'-'+now,workflowId:p.workflowId,policyVersion:p.policyVersion,
  approvedAt:s.approval.approvedAt,deadline:p.deadline,principalUnits:p.principalUnits,protectedIdleReserveUnits:p.protectedIdleReserveUnits,maxMovableUnits:p.maxMovableUnits,
  poolCapacityUnits:String(100n*10n**18n),minNetBenefitMicrounits:p.minNetBenefitMicrounits,maxFeeMicrounits:p.maxFeeMicrounits,
  maxObservationAgeSeconds:p.maxObservationAgeSeconds,maxObservationSkewSeconds:p.maxObservationSkewSeconds,cooldownSeconds:'86400',fictionalPrices:p.fictionalPrices,
  controlledSourceRateRay:p.chains[0].controlledRateRay,controlledDestinationRateRay:phase==='hold'?p.chains[0].controlledRateRay:p.chains[1].controlledRateRay,
  estimatedSourceGasWei:String(600000n*sourceGasPrice),estimatedDestinationApprovalGasWei:'0',
  exitIfStayMicrounits:'0',exitIfMoveMicrounits:'0',sourceLinkFundingUnits:'1000000000000000000',destinationLinkFundingUnits:'1000000000000000000'};
 const planPath=resolve(runDir,'approved-scenario.json');await save(planPath,completed);
 const reviewDir=resolve(runDir,'review');await command('scripts/lending-plan.mjs',[planPath,reviewDir]);
 const seedPath=resolve(reviewDir,'seed-run.json'),seed=await load(seedPath);
 // These are newly assembled scenario observations. No policy deadline, limits,
 // live block timestamps or previously immutable input are rewritten.
 for(const e of observations(seed.snapshot))e.observedAt=scenarioTime;
 // Static fictional prices and controlled cost assumptions apply at the snapshot's
 // finalized block times. Real contract timestamps are supplied by prepare below.
 // The current RPC gas-price observation is recorded separately and is NOT the
 // assumed future gas price; the budget uses already observed setup prices.
 seed.policy.approvalId=s.approval.planHash;seed.context.walletBindingId='signed-setup-'+s.approval.planHash.slice(2,18);
 seed.snapshot.costs.exitIfStay.evidence.source='controlled-assumption:equal-zero-exit-costs-for-demonstration';
 seed.snapshot.costs.exitIfMove.evidence.source='controlled-assumption:equal-zero-exit-costs-for-demonstration';
 const deposit=s.records['11155111-deposit'];
 seed.snapshot.accounting.initialDepositCostMicrounits=String((BigInt(deposit.gasUsed)*BigInt(deposit.effectiveGasPrice)*BigInt(p.fictionalPrices.ethMicrounitsPerToken)+10n**18n-1n)/10n**18n);
 await writeFile(seedPath,JSON.stringify(seed,null,2)+'\n');
 await save(resolve(runDir,'fee-assumptions.json'),{observedAt:now,sourceGasPriceWei:String(sourceGasPrice),rpcGasPriceWei:rpcGasPrices.map(String),scenarioAsOf:scenarioTime,finalizedBlocks:finalizedBlocks.map(b=>({number:String(b.number),timestamp:String(b.timestamp),hash:b.hash})),gasPriceAssumption:'controlled budget using maximum effective gas prices of finalized setup receipts on each chain; current RPC prices are informational only',sourceAssumedGasUnits:'600000',
  sourceAmountWei:completed.estimatedSourceGasWei,destinationGasPriceWei:String(destinationGasPrice),destinationApproval:'included in the router bridge quote',
  exitCosts:'controlled equal-zero scenario assumption; not actual withdrawal fee quotes',fictionalPrices:p.fictionalPrices,approval:s.approval.planHash});
 const inputPath=resolve(runDir,'immutable-input.json');
 await command('scripts/lending-testnet.mjs',['prepare',resolve(dir,'manifest.json'),seedPath,inputPath]);
 const evidencePath=resolve(dir,phase+'-evidence.json');
 await command('scripts/lending-testnet.mjs',['simulate',inputPath,evidencePath]);
 const evidence=await load(evidencePath);
 if(evidence.actual?.result?.decision.action!==phase)throw Error('The decision is '+evidence.actual?.result?.decision.action+'; inspect its reason. Policy limits were preserved.');
 console.log('PART2_'+phase.toUpperCase()+'_VERIFIED: deployed reads, unchanged balances and exact local parity. Evidence: '+evidencePath);
}catch(e){console.error(e.shortMessage||e.message);process.exitCode=1;}
