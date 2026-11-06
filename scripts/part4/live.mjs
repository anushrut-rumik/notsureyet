// Application-owned testnet execution. Invoked only by the authenticated policy service.
// Signing material arrives over stdin from the existing encrypted wallet, never argv or logs.
import {readFile,writeFile,mkdir,chmod} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {native} from './rules.mjs';
import {homedir} from 'node:os';
import {existsSync} from 'node:fs';
import {spawn} from 'node:child_process';
import {DatabaseSync} from 'node:sqlite';
import {createPublicClient,http,parseAbi,encodeFunctionData,encodeAbiParameters,parseAbiParameters,keccak256,toBytes} from 'viem';
import {privateKeyToAccount} from 'viem/accounts';
import {testnets} from '../../.local/lending-build/testnets.js';
import {createRunInput} from '../../.local/lending-build/schema.js';
import {evaluateRun} from '../../.local/lending-build/run.js';
import {buildInstruction,signingRequest} from '../../.local/lending-build/part3/report.js';
import {refresh} from '../part3/cli.mjs';
import {OperationJournal} from '../part3/journal.mjs';
import {reconcile,finalizedReceipt,resolveNoSend} from '../part3/monitor.mjs';
import {verifyRuntimeCode} from '../part3/code.mjs';
import {verifySetupExecution} from '../lib/lending-receipts.mjs';
const check=(ok,m)=>{if(!ok){const e=Error(m);e.name='LendingExecutionError';throw e;}};
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const abi=parseAbi(['function owner() view returns(address)','function pool() view returns(address)',
 'function asset() view returns(address)','function peer() view returns(address)','function protectedReserve() view returns(uint256)',
 'function maxMove() view returns(uint256)','function activeOperation() view returns(bytes32)',
 'function binding() view returns(bytes32)',
 'function reservedInbound() view returns(uint256)','function policyVersion() view returns(bytes32)',
 'function setPolicyVersion(bytes32)','function balanceOf(address) view returns(uint256)',
 'function transfer(address,uint256) returns(bool)','function depositToPool(uint256)',
 'function recoverFailedDeposit(bytes32)','function withdrawToOwner(uint256,uint256)','function settleOperation(bytes32,bytes32)',
 'function observe() view returns(uint256[12])']);
export async function executeLive(request,dependencies={}) {
let db,journal,result;
try {
 const {manifest,plan,action,directory,user}=request;
 check(['start','evaluate','monitor','withdraw','pause'].includes(action),'Unsupported live action');
 check(manifest.scope==='dedicated-test-only'&&manifest.chains.length===2&&new Set(manifest.chains.map(c=>c.chainId)).size===2,'Both testnets required');
 const signerKey=request.key,account=privateKeyToAccount(signerKey);delete request.key;
 check(same(account.address,manifest.owner)&&same(account.address,plan.policy.walletAddress)&&plan.policy.userId===user,'Wallet/user binding mismatch');
 check(plan.approvalEvent&&plan.policy.approvalId.startsWith('whatsapp-event:'),'Exact incoming policy approval required');
 const now=()=>String(Math.floor(Date.now()/1000));
 const active=()=>{check(['approved','awaiting_deposit','active'].includes(plan.status),'Policy stopped');check(BigInt(now())<BigInt(plan.policy.deadline),'Policy expired');};
 if(['start','evaluate'].includes(action))active();
 await mkdir(directory,{recursive:true,mode:0o700});
 db=new DatabaseSync(resolve(directory,'transactions.sqlite'));await chmod(resolve(directory,'transactions.sqlite'),0o600);
 db.exec('PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS txs(id TEXT PRIMARY KEY, chain TEXT, hash TEXT, raw TEXT, intent TEXT, state TEXT);');
 if(!db.prepare('PRAGMA table_info(txs)').all().some(c=>c.name==='receipt'))db.exec('ALTER TABLE txs ADD COLUMN receipt TEXT');
 journal=new OperationJournal(resolve(directory,'operations.sqlite'));
 const clients=new Map(testnets.map(n=>[n.chainId,createPublicClient({transport:http(n.rpc,{timeout:15000,retryCount:1})})]));
 const client=id=>{check(clients.has(id),'Unsupported chain');return dependencies.clientFor?dependencies.clientFor(id):clients.get(id);};
 const walletArtifact=JSON.parse(await readFile('contracts/out/ExecutionStrategyWallet.sol/ExecutionStrategyWallet.json'));
 const poolArtifact=JSON.parse(await readFile('contracts/out/MockPool.sol/MockPool.json'));
 check(walletArtifact.metadata.settings.evmVersion==='cancun'&&poolArtifact.metadata.settings.evmVersion==='cancun',
  'Run npm run lending:setup:build to restore the reviewed Cancun contract artifacts');
 const observations=[];
 for(const c of manifest.chains){
  const rpc=client(c.chainId),n=testnets.find(n=>n.chainId===c.chainId),peer=manifest.chains.find(x=>x!==c);
  check(String(await rpc.getChainId())===c.chainId,'RPC chain mismatch');
  const block=await rpc.getBlock({blockTag:'finalized'});
  const read=(functionName,args=[])=>rpc.readContract({address:c.wallet,abi,functionName,args,blockNumber:block.number});
  const [code,poolCode,owner,pool,asset,remote,reserve,maximum,v,version,inbound,binding]=await Promise.all([
   rpc.getCode({address:c.wallet,blockNumber:block.number}),rpc.getCode({address:c.pool,blockNumber:block.number}),
   read('owner'),read('pool'),read('asset'),read('peer'),read('protectedReserve'),read('maxMove'),read('observe'),read('policyVersion'),read('reservedInbound'),read('binding')]);
  verifyRuntimeCode(code,walletArtifact);verifyRuntimeCode(poolCode,poolArtifact);
  check(same(owner,account.address)&&same(pool,c.pool)&&same(asset,n.token)&&same(remote,peer.wallet),'Deployment binding mismatch');
  check(reserve===BigInt(plan.policy.protectedIdleReserveUnits)&&maximum===BigInt(plan.policy.maxMovableUnits),'Deployment limits differ from approved policy');
  const expected=keccak256(encodeAbiParameters(parseAbiParameters('address,address,address,address,address,address,bytes32,bytes32,uint256,uint256,address'),
   [account.address,c.pool,n.token,n.link,n.router,n.forwarder,manifest.workflowId,version,reserve,maximum,peer.wallet]));
  check(same(binding,expected),'Strategy token, router, forwarder or workflow binding mismatch');
  observations.push({...c,values:v.map(String),version,inbound:String(inbound),blockNumber:String(block.number),blockHash:block.hash,
   binding,tokenDecimals:18,linkDecimals:18,quoteLinkUnits:'0'});
 }
 // Every nonterminal signed transaction blocks ALL new submissions, including after restart.
 async function settleTransactions(){
  for(const row of db.prepare("SELECT * FROM txs WHERE state!='confirmed'").all()){
   const receipt=await finalizedReceipt(client(row.chain),row.hash);
   if(!receipt)return false;
   check(receipt.status==='success','A submitted transaction reverted; explicit reconciliation required');
   await verifySetupExecution(client(row.chain),JSON.parse(row.intent),row.hash);
   db.prepare("UPDATE txs SET state='confirmed',receipt=? WHERE id=?").run(JSON.stringify({blockNumber:String(receipt.blockNumber),blockHash:receipt.blockHash,gasUsed:String(receipt.gasUsed),effectiveGasPriceWei:String(receipt.effectiveGasPrice),nativeFeeWei:String(receipt.gasUsed*receipt.effectiveGasPrice)}),row.id);
  }return true;
 }
 async function send(id,chain,to,functionName,args){
  const old=db.prepare('SELECT * FROM txs WHERE id=?').get(id);
  if(old)return old.state==='confirmed';
  check(await settleTransactions(),'Earlier transaction outcome pending');
  if(action!=='withdraw')active();
  const rpc=client(chain),data=encodeFunctionData({abi,functionName,args});
  const [latest,pending]=await Promise.all(['latest','pending'].map(blockTag=>rpc.getTransactionCount({address:account.address,blockTag})));
  check(latest===pending,'Owner has another pending transaction');
  if(functionName==='transfer'){
   const balance=await rpc.readContract({address:to,abi,functionName:'balanceOf',args:[account.address]});
   check(balance>=args[1],'Agent wallet needs more test tokens on this chain before funding can continue');
  }
  const gas=await rpc.estimateGas({account:account.address,to,data}),gasPrice=await rpc.getGasPrice();
  const bound=BigInt(manifest.maxTransactionGasWei??'0');check(bound>0n,'Configure an explicit native transaction gas cap');
  const limit=(gas*120n+99n)/100n;check(limit*gasPrice<=bound,'Transaction exceeds configured gas cap');
  check(await rpc.getBalance({address:account.address})>=limit*gasPrice,'Insufficient native test gas');
  const signed=await account.signTransaction({chainId:Number(chain),nonce:pending,to,data,value:0n,gas:limit,gasPrice,type:'legacy'});
  const hash=keccak256(signed),intent={chainId:chain,from:account.address,to,data,value:'0',nonce:pending};
  db.prepare('INSERT INTO txs(id,chain,hash,raw,intent,state) VALUES(?,?,?,?,?,?)').run(id,chain,hash,signed,JSON.stringify(intent),'unknown');
  // Commit before network: any lost response is reconciled by the exact signed hash, never repeated.
  try{check(same(await rpc.sendRawTransaction({serializedTransaction:signed}),hash),'Transaction hash mismatch');}catch{}
  return false;
 }
 const ready=await settleTransactions();
 for(const record of journal.list().filter(r=>r.state==='prepared')){
  if(action==='pause'||action==='withdraw'||plan.status==='expired'||BigInt(now())>=BigInt(record.immutable.instruction.expiry))
   journal.update(record.id,'cancelled',{failureReason:'Policy stopped or prepared instruction expired before submission'});
 }
 const pending=journal.pending(account.address);
 let op=pending;
 if(pending&&pending.state!=='prepared'){
  op=await reconcile(journal,pending.id,client);
  if(['submission_unknown','submitted','failed'].includes(op.state)&&BigInt(now())>=BigInt(op.immutable.instruction.expiry)){
   try{op=await resolveNoSend(journal,op.id,client(op.immutable.instruction.sourceChainId));}catch{/* Missing finalized proof leaves the operation pending. */}
  }
 }
 const summaries=()=>journal.list().map(r=>({id:r.id,state:r.state,details:r.details,amountUnits:r.immutable.instruction.amount}));
 const output=extra=>{result={mode:'live-testnet',observations,operations:summaries(),
  transactions:db.prepare('SELECT id,chain,hash,state,receipt FROM txs').all().map(t=>({...t,receipt:t.receipt?JSON.parse(t.receipt):null})),...extra};};
 if(!ready){output({status:'pending_transaction'});}
 else if(action==='pause'){output({status:'paused'});}
 else if(action==='withdraw'&&op?.state==='idle_tokens'){
  const i=op.immutable.instruction,c=observations.find(c=>c.chainId!==i.sourceChainId);
  await send(plan.id+':recover:'+op.id,c.chainId,c.wallet,'recoverFailedDeposit',[op.details.messageId]);output({status:'recovery_pending'});
 }
 else if(action==='monitor'){output({status:op?.state??'idle'});}
 else if(op&&!['verified_deposit','cancelled','recovered_idle'].includes(op.state)){output({status:'pending_transfer'});}
 else if(action==='start'){
  let complete=true;
  for(const c of observations){
   const n=testnets.find(n=>n.chainId===c.chainId),prefix=plan.id+':'+c.chainId;
   const version=keccak256(toBytes(plan.policy.version));
   if(c.version!==version){await send(prefix+':policy',c.chainId,c.wallet,'setPolicyVersion',[version]);complete=false;break;}
   const linkTarget=BigInt(manifest.linkFundingUnits??'0');
   check(linkTarget>0n,'Configure test LINK funding');
   if(BigInt(c.values[8])<linkTarget){await send(prefix+':link',c.chainId,n.link,'transfer',[c.wallet,linkTarget-BigInt(c.values[8])]);complete=false;break;}
   const amount=c.chainId==='11155111'?BigInt(plan.settings.amountUnits):0n;
   const target=amount+BigInt(plan.settings.reserveUnits),idle=BigInt(c.values[5]),invested=BigInt(c.values[4]);
   if(invested!==0n){check(invested===amount,'Unexpected existing principal');continue;}
   if(idle<target){await send(prefix+':fund',c.chainId,n.token,'transfer',[c.wallet,target-idle]);complete=false;break;}
   if(amount>0n){await send(prefix+':deposit',c.chainId,c.wallet,'depositToPool',[amount]);complete=false;break;}
  }
  output({status:complete?'deposit_verified':'funding_pending'});
 } else if(action==='withdraw'){
  let complete=true;
  for(const record of journal.list().filter(r=>['verified_deposit','recovered_idle'].includes(r.state))){
   const c=observations.find(c=>c.chainId===record.immutable.instruction.sourceChainId);
   const current=await client(c.chainId).readContract({address:c.wallet,abi,functionName:'activeOperation'});
   if(same(current,record.id)){await send(plan.id+':settle:'+record.id,c.chainId,c.wallet,'settleOperation',[record.id,record.details.messageId]);complete=false;break;}
  }
  if(complete)for(const c of observations){
   check(c.inbound==='0','Inbound tokens require existing-message recovery first');
   const total=BigInt(c.values[4]),liquidity=BigInt(c.values[6]),principal=total<liquidity?total:liquidity,idle=BigInt(c.values[5]);
   if(principal+idle>0n){await send(plan.id+':withdraw:'+c.chainId+':'+principal+':'+idle,c.chainId,c.wallet,'withdrawToOwner',[principal,idle]);complete=false;break;}
   if(total>0n)complete=false;
  }
  output({status:complete?'withdrawal_verified':'withdrawal_pending'});
 } else {
  active();
  for(const record of journal.list().filter(r=>['verified_deposit','recovered_idle'].includes(r.state))){
   const c=observations.find(c=>c.chainId===record.immutable.instruction.sourceChainId);
   const current=await client(c.chainId).readContract({address:c.wallet,abi,functionName:'activeOperation'});
   if(same(current,record.id)){await send(plan.id+':settle:'+record.id,c.chainId,c.wallet,'settleOperation',[record.id,record.details.messageId]);output({status:'settlement_pending'});return result;}
  }
  const positions=observations.filter(c=>BigInt(c.values[4])>0n);check(positions.length===1,'Exactly one managed position required');
  const source=positions[0],destination=observations.find(c=>c!==source);
  const seed=structuredClone(manifest.seedRun);check(seed,'Configured Part 2 seed input required');
  seed.policy=plan.policy;seed.context={userId:user,walletAddress:account.address,walletBindingId:'live:'+plan.id,policyVersion:plan.policy.version};
  seed.runId=plan.id+':'+now();seed.operationId=seed.runId;
  seed.snapshot.position.userId=user;seed.snapshot.position.walletAddress=account.address;
  seed.snapshot.position.location=plan.policy.approvedPools.find(p=>p.chainId===source.chainId);
  seed.snapshot.position.movableUnits=source.values[4];
  seed.snapshot.currentPool.location=seed.snapshot.position.location;
  seed.snapshot.destinationPool.location=plan.policy.approvedPools.find(p=>p.chainId===destination.chainId);
  seed.snapshot.reserve.chainId=source.chainId;
  for(const fee of seed.snapshot.costs.components)fee.chainId=['destinationApprovalGas','destinationGas','depositFee'].includes(fee.category)?destination.chainId:source.chainId;
  const current=await (dependencies.refresh??refresh)({scenario:'Live Part 4 testnet observations; fictional prices and mock APR',run:createRunInput(seed),workflowId:manifest.workflowId,chains:observations.map(c=>({chainId:c.chainId,wallet:c.wallet,pool:c.pool,blockNumber:c.blockNumber,blockHash:c.blockHash,binding:c.binding,values:c.values,tokenDecimals:18,linkDecimals:18,quoteLinkUnits:'0'}))});
  const evaluated=await (dependencies.evaluate??native)(current.observation.run);
  const decision=evaluated.result;
  check(JSON.stringify(decision)===JSON.stringify(evaluateRun(current.observation.run,()=>decision.decision.evaluatedAt)),'CRE decision differs from shared rules');
  if(decision.decision.action!=='move'){output({status:'no_move',decision});}
  else {
   const expiry=String([BigInt(plan.policy.deadline),BigInt(now())+300n,BigInt(current.observation.chains.find(c=>c.chainId===source.chainId).values[2])+1800n].reduce((a,b)=>a<b?a:b));
   check(BigInt(current.observation.run.snapshot.costs.components.find(c=>c.category==='sourceGas').amountUnits)<=BigInt(manifest.maxTransactionGasWei),'CRE report gas budget exceeds approved native cap');
   const instruction=buildInstruction(current.observation,expiry),payload={observation:current.observation,instruction};
   payload.signature=await account.signTypedData(signingRequest(payload));
   journal.prepare(payload);
   const path=resolve(directory,instruction.operationId+'.json');await writeFile(path,JSON.stringify(payload),{mode:0o600,flag:'wx'});
   active();
   const nonces=await Promise.all(['latest','pending'].map(blockTag=>client(source.chainId).getTransactionCount({address:account.address,blockTag})));
   check(nonces[0]===nonces[1],'Owner has a pending transaction');
   journal.claim(instruction.operationId,{owner:account.address,latestNonce:nonces[0],pendingNonce:nonces[1],sourceChainId:source.chainId});
   const cli=existsSync(resolve(homedir(),'.cre/bin/cre'))?resolve(homedir(),'.cre/bin/cre'):'cre';
   // CRE independently rechecks the signed observations, rules and current contract state before broadcasting.
   // The approved agent wallet is the CLI broadcaster. No command or output exposes its key.
   await (dependencies.broadcast??(async()=>new Promise(resolveDone=>{
    const child=spawn(cli,['workflow','simulate','part3','--target','staging-settings','--non-interactive',
     '--trigger-index','0','--http-payload',path,'--broadcast'],{cwd:resolve('cre-lending'),
      env:{...process.env,CRE_ETH_PRIVATE_KEY:signerKey,PATH:resolve('node_modules/.bin')+':'+process.env.PATH},stdio:'ignore'});
    const timeout=setTimeout(()=>child.kill('SIGKILL'),120000);
    child.on('error',()=>{clearTimeout(timeout);resolveDone();});
    child.on('close',()=>{clearTimeout(timeout);resolveDone();});
   }))) (path);
   output({status:'submission_unknown',decision});
  }
 }
 return result;
} finally{try{journal?.close();db?.close();}catch{}}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 let raw='';for await(const c of process.stdin){raw+=c;check(raw.length<200000,'Request too large');}
 try {const request=JSON.parse(raw);raw='';console.log(JSON.stringify(await executeLive(request)));}
 catch(e){console.log(JSON.stringify({error:e.name==='LendingExecutionError'?e.message:'Live execution stopped. Verify deployment bindings, policy, funding and pending receipts.',failureClass:e.name}));process.exitCode=1;}
}
