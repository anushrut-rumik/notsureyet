// User-run setup for the existing encrypted agent owner. Fixed testnet artifacts only.
import {readFile,writeFile,mkdir,chmod} from 'node:fs/promises';
import {resolve} from 'node:path';
import {privateKeyToAccount} from 'viem/accounts';
import {keccak256,toBytes} from 'viem';
import {DatabaseSync} from 'node:sqlite';
import {buildSetupPlan,clientFor} from '../lib/lending-setup.mjs';
import {verifySetupExecution} from '../lib/lending-receipts.mjs';
import {finalizedReceipt} from '../part3/monitor.mjs';
import {exampleRun} from '../../.local/lending-build/fixtures.js';
let text='';for await(const chunk of process.stdin)text+=chunk;
let db;
try{
 const request=JSON.parse(text);text='';const {directory,owner,user,confirmation}=request;
 await mkdir(directory,{recursive:true,mode:0o700});const path=resolve(directory,'review.json');
 const artifacts={pool:JSON.parse(await readFile('contracts/out/MockPool.sol/MockPool.json')),
  wallet:JSON.parse(await readFile('contracts/out/ExecutionStrategyWallet.sol/ExecutionStrategyWallet.json'))};
 if(artifacts.pool.metadata.settings.evmVersion!=='cancun'||artifacts.wallet.metadata.settings.evmVersion!=='cancun')throw Error('Build Cancun artifacts');
 let plan;try{plan=JSON.parse(await readFile(path));}catch(e){if(e.code!=='ENOENT')throw e;
  const nonces={};for(const chain of ['11155111','84532']){
   const rpc=clientFor(chain);if(String(await rpc.getChainId())!==chain)throw Error('RPC mismatch');
   const a=await rpc.getTransactionCount({address:owner,blockTag:'latest'}),b=await rpc.getTransactionCount({address:owner,blockTag:'pending'});
   if(a!==b)throw Error('Owner pending transaction');nonces[chain]=a;
  }
  plan=buildSetupPlan(owner,nonces,artifacts,Math.floor(Date.now()/1000));
  plan.actions=plan.actions.filter(a=>a.creates||a.id.endsWith('-peer'));
  plan.purpose='Deploy two mock pools and two execution wallets; pair them. No token funding or deposit until WhatsApp policy approval.';
  await writeFile(path,JSON.stringify(plan),{mode:0o600,flag:'wx'});
 }
 if(plan.compiler.walletBytecodeHash!==keccak256(artifacts.wallet.bytecode.object)||plan.compiler.poolBytecodeHash!==keccak256(artifacts.pool.bytecode.object))throw Error('Review bytecode differs from current artifacts');
 if(plan.owner.toLowerCase()!==owner.toLowerCase())throw Error('Review owner mismatch');
 const hash=keccak256(toBytes(JSON.stringify(plan)));
 if(!confirmation){console.log(JSON.stringify({reviewHash:hash,owner,purpose:plan.purpose,
  chains:plan.chains.map(c=>({chainId:c.chainId,pool:c.pool,wallet:c.wallet})),
  actions:plan.actions.map(a=>({chainId:a.chainId,id:a.id,title:a.title,to:a.to,nonce:a.nonce})),
  controlledRates:'4% source, 10,000% destination; mock display only, no accrued interest',
  maxGasPerTransactionWei:'10000000000000000',userAction:'Run this same launcher with --execute '+hash}));
 }else{
  if(confirmation!==hash)throw Error('Exact review confirmation required');
  const account=privateKeyToAccount(request.key);delete request.key;
  if(account.address.toLowerCase()!==owner.toLowerCase())throw Error('Signer mismatch');
  db=new DatabaseSync(resolve(directory,'setup.sqlite'));await chmod(resolve(directory,'setup.sqlite'),0o600);
  db.exec('PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,hash TEXT,state TEXT,intent TEXT,raw TEXT)');
  let complete=true;
  for(const action of plan.actions){
   const rpc=clientFor(action.chainId);if(String(await rpc.getChainId())!==action.chainId)throw Error('Wrong chain');
   const prior=db.prepare('SELECT * FROM actions WHERE id=?').get(action.id);
   if(prior){if(prior.state==='confirmed')continue;
    const receipt=await finalizedReceipt(rpc,prior.hash);
    if(!receipt){console.log(JSON.stringify({status:'pending',action:action.id,hash:prior.hash}));complete=false;break;}
    if(receipt.status!=='success')throw Error('Setup transaction reverted; do not retry');
    await verifySetupExecution(rpc,action,prior.hash);
    if(action.creates&&receipt.contractAddress?.toLowerCase()!==action.creates.toLowerCase())throw Error('Deployment address mismatch');
    db.prepare("UPDATE actions SET state='confirmed' WHERE id=?").run(action.id);continue;
   }
   if(request.reconcileOnly){console.log(JSON.stringify({status:'paused'}));complete=false;break;}
   const nonces=await Promise.all(['latest','pending'].map(blockTag=>rpc.getTransactionCount({address:owner,blockTag})));
   if(nonces.some(n=>n!==action.nonce))throw Error('Setup nonce drift; reconcile existing transactions');
   const gas=await rpc.estimateGas({account:owner,to:action.to??undefined,data:action.data,value:0n});
   const gasPrice=await rpc.getGasPrice(),limit=(gas*120n+99n)/100n;
   if(limit*gasPrice>10000000000000000n)throw Error('Setup gas cap exceeded');
   if(await rpc.getBalance({address:owner,blockTag:'pending'})<limit*gasPrice)throw Error('Insufficient setup gas balance');
   const raw=await account.signTransaction({chainId:Number(action.chainId),nonce:action.nonce,to:action.to??undefined,
    data:action.data,value:0n,gas:limit,gasPrice,type:'legacy'}),hash=keccak256(raw);
   db.prepare('INSERT INTO actions VALUES(?,?,?,?,?)').run(action.id,hash,'unknown',JSON.stringify(action),raw);
   try{await rpc.sendRawTransaction({serializedTransaction:raw});}catch{}
   console.log(JSON.stringify({status:'pending',action:action.id,hash}));complete=false;break;
  }
  if(complete){
   const manifest={scope:'dedicated-test-only',owner,workflowId:plan.workflowId,automaticExecution:true,
    maxTransactionGasWei:'10000000000000000',linkFundingUnits:'1000000000000000000',
    chains:plan.chains.map(c=>({chainId:c.chainId,wallet:c.wallet,pool:c.pool})),seedRun:exampleRun(30)};
   // Seed valuation is explicitly fictional; refresh replaces every onchain observation and fee quote.
   manifest.seedRun=structuredClone(manifest.seedRun);
   manifest.seedRun.snapshot.costs.components.forEach(c=>{c.assetId=['sourceGas','sourceApprovalGas','destinationGas','destinationApprovalGas'].includes(c.category)?'ETH_GAS':'LINK';c.assetDecimals=18;c.amountUnits='0';c.priceMicrounitsPerToken=c.assetId==='LINK'?'1000000':'2000000000';});
   manifest.seedRun.snapshot.feeBalances=plan.chains.flatMap(c=>['LINK','ETH_GAS'].map(assetId=>({chainId:c.chainId,assetId,assetDecimals:18,availableUnits:'0',reservedUnits:'0',evidence:{observedAt:'0',source:'setup-placeholder-refreshed-before-execution',simulated:true}})));
   await writeFile(resolve(directory,'bindings.json'),JSON.stringify({[user]:manifest},null,2),{mode:0o600});
   console.log(JSON.stringify({status:'complete',bindings:resolve(directory,'bindings.json')}));
  }
 }
}catch{console.log(JSON.stringify({error:'Setup stopped. Verify the exact review, testnet funding, wallet binding and existing transaction receipts.'}));process.exitCode=1;}
finally{db?.close();}
