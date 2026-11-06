// Read-only tooling. This file never creates a wallet, signs, mints or submits RPC transactions.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {homedir} from 'node:os';
import {createPublicClient,http,parseAbi,encodeAbiParameters,parseAbiParameters,keccak256,toBytes} from 'viem';
import {testnets} from '../.local/lending-build/testnets.js';
import {readAbi,payloadSchema,validateObservedInput} from '../.local/lending-build/observation.js';
import {createRunInput} from '../.local/lending-build/schema.js';
import {readLoggedResult} from '../.local/lending-build/cre-evidence.js';
import {evaluateRun} from '../.local/lending-build/run.js';
import {z} from 'zod';
import {verifySetupExecution} from './lib/lending-receipts.mjs';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const abi=parseAbi(['function decimals() view returns (uint8)','function balanceOf(address) view returns (uint256)',
 'function isChainSupported(uint64) view returns (bool)','function getOnRamp(uint64) view returns (address)',
 'function typeAndVersion() view returns (string)','function getPool(address) view returns (address)',
 'function getRemoteToken(uint64) view returns (bytes)']);
const registries=['0x95F29FEE11c5C55d26cCcf1DB6772DE953B37B82','0x736D0bBb318c1B27Ff686cd19804094E66250e17'];
const clients=testnets.map(n=>createPublicClient({transport:http(n.rpc,{timeout:15000,retryCount:1})}));
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const check=(ok,message)=>{if(!ok)throw Error(message);};
const load=async path=>JSON.parse(await readFile(path,'utf8'));
const save=async(path,value)=>{await mkdir(dirname(resolve(path)),{recursive:true});await writeFile(path,JSON.stringify(value,null,2)+'\n',{flag:'wx'});};
const address=z.string().regex(/^0x[\da-fA-F]{40}$/).refine(s=>BigInt(s)!==0n);
const hash=z.string().regex(/^0x[\da-fA-F]{64}$/).refine(s=>BigInt(s)!==0n);
const manifestSchema=z.object({scope:z.literal('dedicated-test-only'),owner:address,workflowId:hash,
 chains:z.array(z.object({chainId:z.enum(['11155111','84532']),pool:address,wallet:address,
  poolDeploymentTx:hash,walletDeploymentTx:hash,fundingTxs:z.array(hash).min(1),
  fundingCalls:z.array(z.object({hash,to:address,data:z.string().regex(/^0x(?:[\da-fA-F]{2})+$/)}).strict()).optional()}).strict()).length(2)}).strict();
async function preflight(owner){
 return Promise.all(testnets.map(async(n,i)=>{
  const c=clients[i],other=testnets[1-i];
  check(await c.getChainId()===Number(n.chainId),'RPC actual chain ID mismatch');
  for(const a of [n.router,n.token,n.link,n.forwarder])check((await c.getCode({address:a}))?.length>2,'Infrastructure has no code');
  const read=(address,functionName,args)=>c.readContract({address,abi,functionName,args});
  check(await read(n.token,'decimals')===18 && await read(n.link,'decimals')===18,'Unexpected token decimals');
  check(await read(n.router,'isChainSupported',[BigInt(other.selector)]),'Direct lane not supported');
  const onRamp=await read(n.router,'getOnRamp',[BigInt(other.selector)]);
  const version=await read(onRamp,'typeAndVersion');
  check(version.includes('2.0.0'),'OnRamp changed: review extraArgs before proceeding');
  const tokenPool=await read(registries[i],'getPool',[n.token]);
  check(BigInt(await read(tokenPool,'getRemoteToken',[BigInt(other.selector)]))===BigInt(other.token),'Remote BnM does not match');
  const block=await c.getBlock({blockTag:'finalized'});
  const funding=owner?{owner,nativeWei:String(await c.getBalance({address:owner})),
   bnmUnits:String(await read(n.token,'balanceOf',[owner])),linkUnits:String(await read(n.link,'balanceOf',[owner]))}:null;
  return {...n,actualChainId:n.chainId,tokenDecimals:18,linkDecimals:18,onRamp,version,tokenPool,
   finalizedBlock:String(block.number),finalizedHash:block.hash,checkedAt:new Date().toISOString(),funding};
 }));
}
async function deployment(manifest,run){
 check(new Set(manifest.chains.map(c=>c.chainId)).size===2,'Both testnets required');
 check(same(manifest.owner,run.context.walletAddress),'Policy owner differs from dedicated signer');
 const reads=await Promise.all(testnets.map(async(n,i)=>{
  const c=clients[i],d=manifest.chains.find(x=>x.chainId===n.chainId),other=manifest.chains.find(x=>x.chainId!==n.chainId);
  check(await c.getChainId()===Number(n.chainId),'RPC chain ID mismatch');
  const block=await c.getBlock({blockTag:'finalized'});
  for(const [tx,address] of [[d.poolDeploymentTx,d.pool],[d.walletDeploymentTx,d.wallet],...d.fundingTxs.map(t=>[t,null])]){
   const r=await c.getTransactionReceipt({hash:tx});
   check(r.status==='success' && r.blockNumber<=block.number,'Missing successful finalized receipt');
   check((await c.getBlock({blockNumber:r.blockNumber})).hash===r.blockHash,'Noncanonical receipt');
   if(address){check(same(r.from,manifest.owner),'Deployment must be sent by the owner');check(same(r.contractAddress,address),'Creation receipt address mismatch');}
   else {
    const action=d.fundingCalls?.find(a=>same(a.hash,tx));
    if(action)await verifySetupExecution(c,{chainId:n.chainId,from:manifest.owner,to:action.to,data:action.data},tx);
    else check(same(r.from,manifest.owner),'Relayed funding requires its exact reviewed call in the manifest');
   }
  }
  for(const a of [d.wallet,d.pool])check((await c.getCode({address:a,blockNumber:block.number}))?.length>2,'Our contract has no finalized code');
  const read=(address,functionName,args)=>c.readContract({address,abi:readAbi,functionName,args,blockNumber:block.number});
  const [values,binding,tokenDecimals,linkDecimals,quote]=await Promise.all([
   read(d.wallet,'observe'),read(d.wallet,'binding'),read(n.token,'decimals'),read(n.link,'decimals'),
   read(d.wallet,'quoteLink',[BigInt(run.snapshot.position.movableUnits)])]);
  const expected=keccak256(encodeAbiParameters(parseAbiParameters('address,address,address,address,address,address,bytes32,bytes32,uint256,uint256,address'),
   [manifest.owner,d.pool,n.token,n.link,n.router,n.forwarder,manifest.workflowId,keccak256(toBytes(run.policy.version)),
    BigInt(run.policy.protectedIdleReserveUnits),BigInt(run.policy.maxMovableUnits),other.wallet]));
  check(same(binding,expected),'Wallet pool/asset/router/forwarder/policy/reserve/limit/peer binding mismatch');
  return {...d,blockNumber:String(block.number),blockHash:block.hash,binding,values:values.map(String),
   tokenDecimals,linkDecimals,quoteLinkUnits:String(quote)};
 }));
 return reads.map(({poolDeploymentTx,walletDeploymentTx,fundingTxs,...c})=>c);
}
async function prepare(manifestPath,seedPath,output){
 const manifest=manifestSchema.parse(await load(manifestPath));
 const seed=createRunInput(await load(seedPath));
 await preflight(manifest.owner);
 const chains=await deployment(manifest,seed);
 const input=JSON.parse(JSON.stringify(seed)),s=input.snapshot;
 const source=chains.find(c=>c.chainId===s.position.location.chainId),dest=chains.find(c=>c.chainId===s.destinationPool.location.chainId);
 check(source && dest && source!==dest,'Position must cross the two testnets');
 const evidence=(c,label)=>({observedAt:c.values[2],source:`testnet:${c.chainId}:block:${c.blockNumber}:${label}`,simulated:label.includes('mock-APR')});
 s.position.investedUnits=source.values[4]; s.position.evidence=evidence(source,'mock-principal');
 check(BigInt(s.position.movableUnits)<=BigInt(s.position.investedUnits),'Requested principal exceeds actual deposit; choose a real funded amount');
 s.reserve.idleBalanceUnits=source.values[5]; s.reserve.evidence=evidence(source,'idle-BnM');
 s.currentPool.aprBps=String(BigInt(source.values[3])/10n**23n);
 s.currentPool.withdrawalAvailableUnits=String(BigInt(source.values[4])<BigInt(source.values[6])?BigInt(source.values[4]):BigInt(source.values[6]));
 s.currentPool.evidence=evidence(source,'controlled-mock-APR-and-liquidity');
 s.destinationPool.aprBps=String(BigInt(dest.values[3])/10n**23n);
 s.destinationPool.depositCapacityUnits=dest.values[7];s.destinationPool.evidence=evidence(dest,'controlled-mock-APR-and-capacity');
 s.valuation.tokenDecimals=source.tokenDecimals;
 const bridge=s.costs.components.find(c=>c.category==='bridgeFee');
 check(bridge.assetId==='LINK' && bridge.assetDecimals===18,'Seed must explicitly price LINK, not a fixture fee asset');
 bridge.amountUnits=source.quoteLinkUnits;bridge.evidence=evidence(source,'router-getFee-LINK-original-units');
 for(const c of chains)for(const [asset,index] of [['LINK',8],['ETH_GAS',9]]){
  const f=s.feeBalances.find(f=>f.chainId===c.chainId&&f.assetId===asset);
  check(f,'Seed must include LINK and ETH_GAS funding per chain');f.availableUnits=c.values[index];f.evidence=evidence(c,asset+'-fee-funding');
 }
 input.observation.assembledAt=String(Math.floor(Date.now()/1000));
 const payload=payloadSchema.parse({scenario:'testnet contracts; controlled mock rates; fictional valuations; estimated gas',run:input,workflowId:manifest.workflowId,chains});
 validateObservedInput(payload);
 await save(output,payload);
 console.log(JSON.stringify({input:resolve(output),runId:input.runId,principal:s.position.investedUnits,idle:s.reserve.idleBalanceUnits,
  bridgeQuoteLINKUnits:source.quoteLinkUnits,localResult:evaluateRun(input,()=>input.observation.assembledAt)},null,2));
}
async function balances(payload){
 return Promise.all(payload.chains.map(async d=>{
  const i=testnets.findIndex(n=>n.chainId===d.chainId),c=clients[i];
  check(await c.getChainId()===Number(d.chainId),'RPC chain ID mismatch');
  const v=await c.readContract({address:d.wallet,abi:readAbi,functionName:'observe'});
  return {chainId:d.chainId,wallet:d.wallet,invested:String(v[4]),idle:String(v[5]),poolToken:String(v[6]),
   walletLink:String(v[8]),ownerGas:String(v[9]),walletGas:String(v[10])};
 }));
}
async function simulate(inputPath,evidencePath){
 const payload=payloadSchema.parse(await load(inputPath));validateObservedInput(payload);
 const before=await balances(payload);
 const args=['workflow','simulate','workflow','--target','staging-settings','--non-interactive','--trigger-index','0','--http-payload',resolve(inputPath)];
 const cli=process.env.CRE_CLI_PATH || (existsSync(resolve(homedir(),'.cre/bin/cre'))?resolve(homedir(),'.cre/bin/cre'):'cre');
 const child=spawnSync(cli,args,{cwd:resolve(root,'cre-lending'),encoding:'utf8',timeout:180000,
  env:{...process.env,PATH:resolve(root,'node_modules/.bin')+':'+process.env.PATH},maxBuffer:16*1024*1024});
 const after=await balances(payload);
 check(JSON.stringify(before)===JSON.stringify(after),'Balances changed during dry run; investigate concurrent transactions');
 const output=(child.stdout||'')+(child.stderr||'');
 let actual=null;let evidenceError=null;
 try{actual=readLoggedResult(output);}catch(error){evidenceError=error.message;}
 const local=actual?evaluateRun(payload.run,()=>actual.result.decision.evaluatedAt):null;
 const matches=actual!==null&&JSON.stringify(local)===JSON.stringify(actual.result);
 const evidence={command:'cd cre-lending && cre '+args.join(' '),exitCode:child.status,balancesUnchanged:true,before,after,
  localMatchesCRE:matches,evidenceError,actual,local,output};
 await save(evidencePath,evidence);
 check(child.status===0&&matches,'CRE did not complete with a matching decision; see evidence output');
 console.log(JSON.stringify({evidence:resolve(evidencePath),balancesUnchanged:true,localMatchesCRE:true,decision:actual.result.decision.action}));
}
const [command,...args]=process.argv.slice(2);
try{
 if(command==='preflight')console.log(JSON.stringify(await preflight(args[0]),null,2));
 else if(command==='prepare'&&args.length===3)await prepare(...args);
 else if(command==='simulate'&&args.length===2)await simulate(...args);
 else throw Error('Usage: lending-testnet.mjs preflight [public-test-owner] | prepare manifest.json seed-run.json immutable-output.json | simulate immutable-input.json evidence.json');
}catch(error){console.error(error.shortMessage||error.message);process.exitCode=1;}
