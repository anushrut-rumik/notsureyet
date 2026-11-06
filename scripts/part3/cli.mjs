// Public artifacts and read-only monitoring. No signing key, wallet client, RPC send or broadcast.
import {readFile,writeFile,mkdir,chmod} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createPublicClient,http,verifyTypedData,parseAbi,keccak256,toBytes} from 'viem';
import {testnets} from '../../.local/lending-build/testnets.js';
import {payloadSchema,readAbi,validateObservedInput,rayToBps} from '../../.local/lending-build/observation.js';
import {buildInstruction,signingRequest,validateExecutionPayload,inputHash} from '../../.local/lending-build/part3/report.js';
import {evaluateRun} from '../../.local/lending-build/run.js';
import {createRunInput} from '../../.local/lending-build/schema.js';
import {OperationJournal} from './journal.mjs';
import {reconcile,recoveryArtifact,resolveNoSend,settleArtifact} from './monitor.mjs';
import {verifyRuntimeCode} from './code.mjs';
const load=async path=>JSON.parse(await readFile(path,'utf8'));
const save=async(path,v)=>{await mkdir(dirname(resolve(path)),{recursive:true,mode:0o700});await writeFile(path,JSON.stringify(v,(_,v)=>typeof v==='bigint'?String(v):v,2)+'\n',{flag:'wx',mode:0o600});};
const check=(ok,message)=>{if(!ok)throw Error(message);};
const clients=new Map(testnets.map(n=>[n.chainId,createPublicClient({transport:http(n.rpc,{timeout:15000,retryCount:1})})]));
const clientFor=id=>{check(clients.has(id),'Only the two approved testnets are supported');return clients.get(id);};
const extraAbi=parseAbi(['function quoteOperation(bytes32,uint256) view returns(uint256)','function activeOperation() view returns(bytes32)',
 'function consumed(bytes32) view returns(bool)','function getOnRamp(uint64) view returns(address)','function typeAndVersion() view returns(string)',
 'function isChainSupported(uint64) view returns(bool)','function getPool(address) view returns(address)','function getRemoteToken(uint64) view returns(bytes)']);
const registries=['0x95F29FEE11c5C55d26cCcf1DB6772DE953B37B82','0x736D0bBb318c1B27Ff686cd19804094E66250e17'];
export async function verifyRoute(){
 return Promise.all(testnets.map(async(n,index)=>{
  const c=clientFor(n.chainId),other=testnets[1-index],read=(address,functionName,args)=>c.readContract({address,abi:extraAbi,functionName,args});
  check(String(await c.getChainId())===n.chainId,'RPC actual chain ID mismatch');
  const code=await Promise.all([n.router,n.token,n.link,n.forwarder].map(address=>c.getCode({address})));
  check(code.every(value=>value&&value!=='0x'),'Missing testnet infrastructure code');
  check(await read(n.router,'isChainSupported',[BigInt(other.selector)]),'Direct lane unavailable');
  const onRamp=await read(n.router,'getOnRamp',[BigInt(other.selector)]),version=await read(onRamp,'typeAndVersion');
  check(version.includes('2.0.0'),'Installed V3 encoding requires reviewed CCIP 2.0 lane');
  const tokenPool=await read(registries[index],'getPool',[n.token]);
  check(BigInt(await read(tokenPool,'getRemoteToken',[BigInt(other.selector)]))===BigInt(other.token),'Exact remote CCIP-BnM unsupported');
  return {chainId:n.chainId,sourceSelector:n.selector,destinationSelector:other.selector,router:n.router,token:n.token,remoteToken:other.token,onRamp,version,tokenPool,checkedAt:new Date().toISOString()};
 }));
}
export async function refresh(observation){
 const original=payloadSchema.parse(observation);
 const route=await verifyRoute(),p=structuredClone(original),run=p.run,s=run.snapshot;
 check(run.operationId,'An explicit candidate operation ID is required');
 const sourceId=s.position.location.chainId,operationId=keccak256(toBytes(run.operationId));
 p.chains=await Promise.all(original.chains.map(async c=>{
  const client=clientFor(c.chainId),n=testnets.find(n=>n.chainId===c.chainId),block=await client.getBlock({blockTag:'finalized'});
  const [walletCode,poolCode,walletArtifact,poolArtifact]=await Promise.all([
   client.getCode({address:c.wallet,blockNumber:block.number}),client.getCode({address:c.pool,blockNumber:block.number}),
   load('contracts/out/ExecutionStrategyWallet.sol/ExecutionStrategyWallet.json'),load('contracts/out/MockPool.sol/MockPool.json')]);
  verifyRuntimeCode(walletCode,walletArtifact);verifyRuntimeCode(poolCode,poolArtifact);
  const read=(functionName,args,abi=readAbi)=>client.readContract({address:c.wallet,abi,functionName,args,blockNumber:block.number});
  const [values,binding,quote,active,consumed]=await Promise.all([read('observe'),read('binding'),
   read('quoteOperation',[operationId,BigInt(s.position.movableUnits)],extraAbi),read('activeOperation',[],extraAbi),read('consumed',[operationId],extraAbi)]);
  check(BigInt(active)===0n&&!consumed,'Pending or consumed onchain operation; reconcile instead');
  return {...c,blockNumber:String(block.number),blockHash:block.hash,binding,values:values.map(String),quoteLinkUnits:String(quote)};
 }));
 const source=p.chains.find(c=>c.chainId===sourceId),dest=p.chains.find(c=>c.chainId!==sourceId);
 const evidence=(c,label,simulated=false)=>({observedAt:c.values[2],source:`testnet:${c.chainId}:block:${c.blockNumber}:${label}`,simulated});
 s.position.investedUnits=source.values[4];s.position.evidence=evidence(source,'principal');
 s.reserve.idleBalanceUnits=source.values[5];s.reserve.evidence=evidence(source,'idle-reserve');
 s.currentPool.aprBps=rayToBps(BigInt(source.values[3]));s.currentPool.evidence=evidence(source,'controlled-mock-rate',true);
 s.currentPool.withdrawalAvailableUnits=String(BigInt(source.values[4])<BigInt(source.values[6])?BigInt(source.values[4]):BigInt(source.values[6]));
 s.destinationPool.aprBps=rayToBps(BigInt(dest.values[3]));s.destinationPool.depositCapacityUnits=dest.values[7];s.destinationPool.evidence=evidence(dest,'controlled-mock-rate',true);
 for(const c of p.chains)for(const [asset,index] of [['LINK',8],['ETH_GAS',9]]){
  const balance=s.feeBalances.find(f=>f.chainId===c.chainId&&f.assetId===asset);check(balance,'Missing fee asset');
  balance.availableUnits=c.values[index];balance.evidence=evidence(c,asset+'-funding');
 }
 const bridge=s.costs.components.find(c=>c.category==='bridgeFee');bridge.amountUnits=source.quoteLinkUnits;bridge.evidence=evidence(source,'actual-operation-router-getFee');
 // Full source report budget; destination execution/approvals are already charged by getFee.
 const gasPrice=await clientFor(sourceId).getGasPrice(),gas=s.costs.components.find(c=>c.category==='sourceGas');
 check(gas.assetId==='ETH_GAS'&&gas.chainId===sourceId,'Source gas must be priced as source ETH_GAS');
 gas.amountUnits=String(1500000n*gasPrice*2n);gas.evidence={observedAt:String(Math.floor(Date.now()/1000)),
  source:`rpc:${sourceId}:conservative-1.5M-report-gas-at-2x-current-gas-price`,simulated:true};
 const bundled=new Set(['withdrawalFee','sourceApprovalGas','destinationApprovalGas','destinationGas','depositFee']);
 for(const fee of s.costs.components)if(bundled.has(fee.category)){
  fee.amountUnits='0';fee.evidence=evidence(fee.chainId===sourceId?source:dest,fee.category==='destinationGas'||fee.category==='destinationApprovalGas'?'included-in-CCIP-quote':'mock-no-fee-or-approval-bundled');
 }
 // Simulated valuations remain explicit user-supplied assumptions; never relabel them as feeds.
 for(const e of [s.valuation.evidence,s.costs.exitIfStay.evidence,s.costs.exitIfMove.evidence]){
  check(e.simulated,'A live valuation/exit quote must be refreshed by its owning adapter');e.observedAt=source.values[2];
 }
 run.observation.assembledAt=String(Math.floor(Date.now()/1000));
 validateObservedInput(p);return {observation:p,route,gasPriceWei:String(gasPrice)};
}
async function verifyOwner(p){check(await verifyTypedData({...signingRequest(p),address:p.observation.run.context.walletAddress,signature:p.signature}),'Instruction signature does not match the approved dedicated owner');}
function checkFresh(p,current){
 for(const c of current.observation.chains){const prior=p.observation.chains.find(v=>v.chainId===c.chainId);
  check(c.binding===prior.binding&&c.quoteLinkUnits===prior.quoteLinkUnits&&[0,3,4,5,6,7,8,9,10,11].every(k=>c.values[k]===prior.values[k]),'Live state drift; cancel and prepare a new signed input');}
 const gas=x=>x.run.snapshot.costs.components.find(c=>c.category==='sourceGas');
 check(BigInt(gas(current.observation).amountUnits)<=BigInt(gas(p.observation).amountUnits),'Source gas budget no longer sufficient');
}
function commandFor(path){return ['cre','workflow','simulate','part3','--target','staging-settings','--non-interactive','--trigger-index','0','--http-payload',resolve(path)];}
function preflight(record,path){
 const i=record.instruction,p=record.observation;
 return {heading:'Prepared on-chain action for user-run execution',action:'CRE-authenticated full-position rebalance',network:'testnet',
  sourceChain:i.sourceChainId,destinationChain:p.chains.find(c=>c.chainId!==i.sourceChainId).chainId,route:testnets.find(n=>n.chainId===i.sourceChainId).selector+' -> '+i.destinationSelector,
  token:i.token,amount:i.amount,payloadHash:inputHash(record),consumer:i.consumer,destination:i.destination,
  policyVersion:i.policyVersion,expiry:i.expiry,maxLinkFeeUnits:i.maxLinkFee,maxGasPriceWei:i.maxGasPrice,
  protectedIdleReserveUnits:p.run.policy.protectedIdleReserveUnits,costComponents:p.run.snapshot.costs.components,
  method:'CRE report -> onReport -> CCIP -> paired-wallet pool deposit',expectedEffect:'Move the exact approved test principal; retain protected reserve; verify the destination separately',
  nonBroadcastCommand:commandFor(path),userRunBroadcastCommand:[...commandFor(path),'--broadcast'],cwd:resolve('cre-lending'),
  requiredGasBudget:'User must bound source transaction cost to the signed sourceGas budget and fund the actual broadcast account',
  review:'Review this carefully and execute it only from your own wallet-controlled environment.',
  signingBoundary:'This skill never signs or sends. You must sign and broadcast from your own wallet.'};
}
async function main(){
 const [command,...args]=process.argv.slice(2);
 if(command==='route'){console.log(JSON.stringify(await verifyRoute(),null,2));return;}
 if(command==='inspect-existing'&&args.length===1){
  const manifest=await load(args[0]);check(manifest.scope==='dedicated-test-only','Dedicated test manifest required');
  const artifact=await load('contracts/out/ExecutionStrategyWallet.sol/ExecutionStrategyWallet.json');
  const chains=await Promise.all(manifest.chains.map(async c=>{
   const client=clientFor(c.chainId),n=testnets.find(n=>n.chainId===c.chainId);check(String(await client.getChainId())===c.chainId,'RPC chain mismatch');
   const block=await client.getBlock({blockTag:'finalized'}),v=await client.readContract({address:c.wallet,abi:readAbi,functionName:'observe',blockNumber:block.number});
   check(BigInt(v[11])===BigInt(manifest.owner),'Existing wallet owner mismatch');
   const tokenAbi=parseAbi(['function balanceOf(address) view returns(uint256)']);
   const [bnm,link,code]=await Promise.all([client.readContract({address:n.token,abi:tokenAbi,functionName:'balanceOf',args:[manifest.owner]}),
    client.readContract({address:n.link,abi:tokenAbi,functionName:'balanceOf',args:[manifest.owner]}),client.getCode({address:c.wallet,blockNumber:block.number})]);
   let compatible=true,reason=null;try{verifyRuntimeCode(code,artifact);}catch(e){compatible=false;reason=e.message;}
   return {chainId:c.chainId,existingPart2Pool:c.pool,existingPart2Wallet:c.wallet,poolDeploymentTx:c.poolDeploymentTx,walletDeploymentTx:c.walletDeploymentTx,
    blockNumber:String(block.number),blockHash:block.hash,principalUnits:String(v[4]),idleReserveUnits:String(v[5]),walletLinkUnits:String(v[8]),ownerNativeWei:String(v[9]),ownerBnMUnits:String(bnm),ownerLinkUnits:String(link),part3Executable:compatible,compatibilityReason:reason};
  }));
  console.log(JSON.stringify({checkedAt:new Date().toISOString(),owner:manifest.owner,kind:'READ ONLY: existing Part 2 deployment state; not Part 3 delivery evidence',chains,sourceTransactionSent:false,
   blocker:'Existing Part 2 wallets deliberately disable execution. New execution wallets and explicitly reviewed funding/migration remain user-run.'},null,2));return;
 }
 if(command==='observe'&&args.length===3){
  const manifest=await load(args[0]),run=createRunInput(await load(args[1]));
  check(manifest.scope==='dedicated-test-only'&&manifest.owner?.toLowerCase()===run.context.walletAddress.toLowerCase(),'Dedicated manifest owner does not match the approved seed');
  check(manifest.chains?.length===2&&new Set(manifest.chains.map(c=>c.chainId)).size===2,'Exactly both approved testnets required');
  const chains=manifest.chains.map(c=>({chainId:c.chainId,wallet:c.wallet,pool:c.pool,blockNumber:'1',blockHash:'0x'+'00'.repeat(32),binding:'0x'+'00'.repeat(32),values:Array(12).fill('0'),tokenDecimals:18,linkDecimals:18,quoteLinkUnits:'0'}));
  const current=await refresh({scenario:'Part 3 finalized testnet observations; mock APR and fictional valuations',workflowId:manifest.workflowId,run,chains});
  await save(args[2],current.observation);console.log(JSON.stringify({observation:resolve(args[2]),route:current.route,sourceTransactionSent:false},null,2));return;
 }
 if(command==='prepare'&&args.length===2){
  const {observation,route,gasPriceWei}=await refresh(await load(args[0]));
  const now=String(Math.floor(Date.now()/1000)),decision=evaluateRun(observation.run,()=>now);
  const dir=resolve(args[1]);await mkdir(dir,{recursive:false,mode:0o700});
  await save(dir+'/decision.json',{decision,route,gasPriceWei,sourceTransactionSent:false});
  if(decision.decision.action!=='move'){console.log(JSON.stringify({prepared:false,decision:decision.decision,sourceTransactionSent:false}));return;}
  const source=observation.chains.find(c=>c.chainId===observation.run.snapshot.position.location.chainId);
  const expiry=String([BigInt(observation.run.policy.deadline),BigInt(source.values[2])+1800n,BigInt(now)+600n].reduce((a,b)=>a<b?a:b));
  const instruction=buildInstruction(observation,expiry),unsigned={observation,instruction};
  await save(dir+'/unsigned.json',unsigned);await save(dir+'/typed-data.json',signingRequest(unsigned));
  await save(dir+'/review.json',preflight(unsigned,dir+'/signed.json'));
  console.log('Prepared '+dir+'. Sign typed-data.json in your dedicated MetaMask wallet, then use admit. No transaction sent.');return;
 }
 const [db,id,value]=args;if(!db)throw Error('Usage: route | inspect-existing manifest.json | observe manifest.json seed-run.json output.json | prepare observation.json new-dir | admit journal.sqlite signed.json | claim journal.sqlite operation-id payload-output.json | attach journal.sqlite operation-id tx-hash | monitor|watch journal.sqlite [operation-id] | recovery journal.sqlite operation-id | attach-recovery journal.sqlite operation-id tx-hash | resolve-no-send journal.sqlite operation-id | cancel journal.sqlite operation-id | status journal.sqlite');
 const journal=new OperationJournal(resolve(db));await chmod(resolve(db),0o600);
 try{
  if(command==='admit'){
   const p=validateExecutionPayload(await load(id));await verifyOwner(p);
   const current=await refresh(p.observation);checkFresh(p,current);
   check(evaluateRun(p.observation.run,()=>String(Math.floor(Date.now()/1000))).decision.action==='move','Financial policy currently rejects execution');
   check(BigInt(p.instruction.expiry)>BigInt(Math.floor(Date.now()/1000)),'Instruction expired');
   console.log(JSON.stringify(journal.prepare(p),null,2));
  }else if(command==='claim'){
   const r=journal.get(id);check(r?.state==='prepared','Operation is already claimed or unknown');await verifyOwner(r.immutable);
   check(BigInt(r.immutable.instruction.expiry)>BigInt(Math.floor(Date.now()/1000)),'Instruction expired');
   const current=await refresh(r.immutable.observation);
   // New block anchors are harmless; the workflow rechecks all live economics and canonical prior anchors.
   const result=evaluateRun(current.observation.run,()=>String(Math.floor(Date.now()/1000)));
   check(result.decision.action==='move','Fresh financial decision cancels this move');
   for(const c of current.observation.chains){const prior=r.immutable.observation.chains.find(p=>p.chainId===c.chainId);
    check(c.binding===prior.binding&&c.quoteLinkUnits===prior.quoteLinkUnits&&[0,3,4,5,6,7,8,9,10,11].every(k=>c.values[k]===prior.values[k]),'Live state drift; cancel and prepare a new signed input');}
   const signedGas=r.immutable.observation.run.snapshot.costs.components.find(c=>c.category==='sourceGas');
   const newGas=current.observation.run.snapshot.costs.components.find(c=>c.category==='sourceGas');check(BigInt(newGas.amountUnits)<=BigInt(signedGas.amountUnits),'Source gas budget no longer sufficient');
   const client=clientFor(r.immutable.instruction.sourceChainId),owner=r.immutable.observation.run.context.walletAddress;
   const [latestNonce,pendingNonce]=await Promise.all(['latest','pending'].map(blockTag=>client.getTransactionCount({address:owner,blockTag})));
   check(latestNonce===pendingNonce,'Dedicated owner has a pending transaction; reconcile it first');
   await save(value,r.immutable);journal.claim(id,{owner,latestNonce,pendingNonce,sourceChainId:r.immutable.instruction.sourceChainId});console.log(JSON.stringify(preflight(r.immutable,value),null,2));
  }else if(command==='attach'||command==='attach-recovery'){
   check(/^0x[\da-fA-F]{64}$/.test(value)&&BigInt(value)!==0n,'Real nonzero hash required');const r=journal.get(id);check(r,'Unknown operation');
   if(command==='attach'){
    check(['submission_unknown','submitted'].includes(r.state),'No pending submission');
    const tx=await clientFor(r.immutable.instruction.sourceChainId).getTransaction({hash:value});
    const source=testnets.find(n=>n.chainId===r.immutable.instruction.sourceChainId);
    check(tx.from.toLowerCase()===r.wallet&&tx.to?.toLowerCase()===source.forwarder.toLowerCase(),'Hash must be the dedicated owner transaction to the configured CRE forwarder');
    console.log(JSON.stringify(journal.update(id,'submitted',{sourceHash:value,sourceNonce:tx.nonce}),null,2));
   }
   else{check(['idle_tokens','destination_failed'].includes(r.state),'Recovery only for existing failure');console.log(JSON.stringify(journal.update(id,r.state,{recoveryHash:value}),null,2));}
  }else if(command==='monitor'||command==='watch'){
   let delay=5000;
   do{
   const operations=id?[journal.get(id)]:journal.list().filter(r=>!['prepared','verified_deposit','cancelled','recovered_idle'].includes(r.state));
   for(const r of operations){check(r,'Unknown operation');for(const n of testnets)check(String(await clientFor(n.chainId).getChainId())===n.chainId,'Wrong RPC chain');
    try{console.log(JSON.stringify(await reconcile(journal,r.id,clientFor),null,2));}catch(e){console.error('Monitoring remains pending: '+(e.shortMessage||e.message));if(command==='monitor')throw e;}}
   if(command==='monitor'||operations.every(r=>['prepared','verified_deposit','cancelled','recovered_idle'].includes(journal.get(r.id).state)))break;
   await new Promise(resolveWait=>setTimeout(resolveWait,delay));delay=Math.min(delay*2,30000);
   }while(true);
  }else if(command==='recovery'){console.log(JSON.stringify(await recoveryArtifact(journal.get(id),clientFor),null,2));}
  else if(command==='settle'){console.log(JSON.stringify(settleArtifact(journal.get(id)),null,2));}
  else if(command==='resolve-no-send'){
   const r=journal.get(id);check(r,'Unknown operation');const client=clientFor(r.immutable.instruction.sourceChainId);
   check(String(await client.getChainId())===r.immutable.instruction.sourceChainId,'Wrong source RPC chain');
   console.log(JSON.stringify(await resolveNoSend(journal,id,client),null,2));
  }
  else if(command==='cancel'){const r=journal.get(id);check(r?.state==='prepared','Only an unclaimed operation can be cancelled');console.log(JSON.stringify(journal.update(id,'cancelled',{failureReason:'Cancelled before any submission claim'})));}
  else if(command==='status')console.log(JSON.stringify(journal.list(),null,2));else throw Error('Unknown command');
 }finally{journal.close();}
}
if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) main().catch(e=>{console.error(e.shortMessage||e.message);process.exitCode=1;});
