// Public test setup only. No wallet client, key access, signing, or RPC sends.
import {readFile,writeFile,rename} from 'node:fs/promises';
import {createPublicClient,http,encodeDeployData,encodeFunctionData,parseAbi,getContractAddress,keccak256,toBytes,verifyTypedData,formatUnits} from 'viem';
import {testnets} from '../../.local/lending-build/testnets.js';
import {verifySetupExecution} from './lending-receipts.mjs';
export {matchesTransaction} from './lending-receipts.mjs';
const abi=parseAbi(['function setPeer(address)','function transfer(address,uint256) returns (bool)',
 'function depositToPool(uint256)','function setCurrentLiquidityRate(uint256)',
 'function balanceOf(address) view returns (uint256)','function balanceOf(address,address) view returns (uint256)']);
export const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
export const assert=(condition,message)=>{if(!condition)throw Error(message);};
export const clientFor=chainId=>{
 const n=testnets.find(n=>n.chainId===String(chainId));assert(n,'Only Ethereum Sepolia and Base Sepolia are permitted');
 return createPublicClient({transport:http(n.rpc,{timeout:15000,retryCount:1})});
};
export function approvalData(plan){
 return {domain:{name:'Lending Part 2 Setup',version:'1',chainId:11155111},
  types:{SetupPlan:[{name:'planHash',type:'bytes32'},{name:'owner',type:'address'}]},primaryType:'SetupPlan',
  message:{planHash:keccak256(toBytes(JSON.stringify(plan))),owner:plan.owner}};
}
export function buildSetupPlan(owner,nonces,artifacts,now){
 assert(/^0x[\da-fA-F]{40}$/.test(owner)&&BigInt(owner)!==0n,'A public test owner is required');
 const version='p2-metamask-'+now,workflowId=keccak256(toBytes('lending-p2-test-workflow-'+now));
 const principal=800000000000000000n,reserve=200000000000000000n,link=10n**18n;
 const chains=testnets.map((n,i)=>{
  const nonce=nonces[n.chainId];assert(Number.isSafeInteger(nonce)&&nonce>=0,'Invalid account nonce');
  return {...n,startNonce:nonce,pool:getContractAddress({from:owner,nonce:BigInt(nonce)}),
   wallet:getContractAddress({from:owner,nonce:BigInt(nonce+1)}),principalUnits:i?'0':String(principal),
   idleUnits:String(reserve),linkUnits:String(link),controlledRateRay:i?'100000000000000000000000000000':'40000000000000000000000000'};
 });
 const actions=[];
 function action(n,id,title,description,to,data,extra={}){
  const nonce=n.startNonce+actions.filter(a=>a.chainId===n.chainId).length;
  actions.push({id,chainId:n.chainId,network:n.chainId==='11155111'?'Ethereum Sepolia':'Base Sepolia',title,description,
   from:owner,to,data,value:'0x0',nonce,...extra});
 }
 for(const n of chains){
  action(n,n.chainId+'-pool','Deploy your mock pool','Create a new principal-only test pool. The displayed rate produces no interest.',null,
   encodeDeployData({abi:artifacts.pool.abi,bytecode:artifacts.pool.bytecode.object,args:[owner,n.token,BigInt(n.controlledRateRay),100n*10n**18n]}),{creates:n.pool});
  action(n,n.chainId+'-wallet','Deploy your strategy wallet','Create a wallet owned only by your MetaMask account, with a 0.2 BnM protected idle reserve.',null,
   encodeDeployData({abi:artifacts.wallet.abi,bytecode:artifacts.wallet.bytecode.object,args:[owner,n.pool,n.link,n.router,n.forwarder,workflowId,keccak256(toBytes(version)),reserve,principal]}),{creates:n.wallet});
 }
 for(const n of chains){
  const peer=chains.find(c=>c.chainId!==n.chainId);
  action(n,n.chainId+'-peer','Bind your opposite-chain wallet','Record the paired wallet for this same test user. This does not bridge anything.',n.wallet,
   encodeFunctionData({abi,functionName:'setPeer',args:[peer.wallet]}));
  const amount=BigInt(n.principalUnits)+reserve;
  action(n,n.chainId+'-bnm','Fund wallet with '+formatUnits(amount,18)+' BnM','Transfer test BnM into your strategy wallet. '+formatUnits(reserve,18)+' BnM stays idle.',n.token,
   encodeFunctionData({abi,functionName:'transfer',args:[n.wallet,amount]}));
  action(n,n.chainId+'-link','Fund wallet with 1 test LINK','Transfer 1 test LINK to your strategy wallet, separately from BnM principal and reserve.',n.link,
   encodeFunctionData({abi,functionName:'transfer',args:[n.wallet,link]}));
  if(BigInt(n.principalUnits)>0n)action(n,n.chainId+'-deposit','Deposit 0.8 BnM into your pool','Make the actual initial deposit; retain 0.2 BnM idle in this strategy wallet.',n.wallet,
   encodeFunctionData({abi,functionName:'depositToPool',args:[principal]}));
 }
 const destination=chains[1];
 action(destination,'hold-rate','Set controlled hold rate to 4%','After the move dry run, set Base mock APR equal to the source. This is setup, not a rebalance.',destination.pool,
  encodeFunctionData({abi,functionName:'setCurrentLiquidityRate',args:[400n*10n**23n]}),{phase:'hold'});
 return {schemaVersion:1,scope:'dedicated-test-only',owner,preparedAt:String(now),deadline:String(BigInt(now)+60n*86400n),
  policyVersion:version,workflowId,principalUnits:String(principal),protectedIdleReserveUnits:String(reserve),maxMovableUnits:String(principal),
  minNetBenefitMicrounits:'1000000',maxFeeMicrounits:'3000000',maxObservationAgeSeconds:'1800',maxObservationSkewSeconds:'900',
  fictionalPrices:{unit:'SIM_USD',bnmMicrounitsPerToken:'1000000',linkMicrounitsPerToken:'1000000',ethMicrounitsPerToken:'2000000000'},
  scenario:'Controlled demonstration only: source mock APR 4%; destination mock APR 10,000% for move, then 4% for hold. No interest accrues.',
  compiler:{solidity:'0.8.26',evmVersion:'cancun',poolBytecodeHash:keccak256(artifacts.pool.bytecode.object),walletBytecodeHash:keccak256(artifacts.wallet.bytecode.object)},
  chains,actions};
}
export function nextAction(state){
 return state.plan.actions.find(a=>state.records[a.id]?.status!=='confirmed')||null;
}
export function actionNonce(state,action){
 // The signed CREATE sequence stays fixed. Later calls use the transport nonce
 // left by verified receipts; a relayed call does not consume a native nonce.
 const preceding=state.plan.actions.slice(0,state.plan.actions.indexOf(action));
 const relayed=preceding.filter(a=>a.chainId===action.chainId&&state.records[a.id]?.status==='confirmed'&&state.records[a.id]?.execution?.kind==='relayed').length;
 assert(!action.creates||relayed===0,'Cannot change a reviewed contract creation nonce');
 return action.nonce-relayed;
}
export async function storeState(path,state){
 await writeFile(path+'.tmp',JSON.stringify(state,null,2)+'\n',{mode:0o600});await rename(path+'.tmp',path);
}
export function createSetupStore(state,path,clients=clientFor){
 let queue=Promise.resolve();
 const serial=fn=>{const result=queue.then(fn);queue=result.catch(()=>{});return result;};
 const persist=()=>path?storeState(path,state):Promise.resolve();
 const current=id=>{const action=nextAction(state);assert(action?.id===id,'Finish or reconcile the preceding action first');return action;};
 const chain=async action=>{const c=clients(action.chainId);assert(String(await c.getChainId())===action.chainId,'RPC actual chain ID mismatch');return c;};
 return {
  state,
  approve:signature=>serial(async()=>{
   assert(!state.approval,'This setup plan is already approved');
   assert(await verifyTypedData({...approvalData(state.plan),address:state.plan.owner,signature}),'The setup plan signature is not from its owner');
   state.approval={signature,planHash:approvalData(state.plan).message.planHash,approvedAt:String(Math.floor(Date.now()/1000))};await persist();return state.approval;
  }),
  claim:id=>serial(async()=>{
   assert(state.approval,'Review and sign the exact setup plan before transactions');
   assert(state.approval.planHash===approvalData(state.plan).message.planHash && await verifyTypedData({...approvalData(state.plan),address:state.plan.owner,signature:state.approval.signature}),'The approved plan changed; do not send');
   assert(BigInt(Math.floor(Date.now()/1000))<BigInt(state.plan.deadline),'The setup plan has expired');
   const action=current(id);assert(!state.records[id]||state.records[id].status==='rejected','A signing request already started. Recover its hash; do not send again.');
   if(action.phase==='hold')assert(state.moveEvidence?.localMatchesCRE===true&&state.moveEvidence?.action==='move','The deployed move dry run must pass before changing the rate');
   const c=await chain(action);
   const [latest,pending]=await Promise.all([c.getTransactionCount({address:state.plan.owner,blockTag:'latest'}),c.getTransactionCount({address:state.plan.owner,blockTag:'pending'})]);
   const nonce=actionNonce(state,action);
   assert(latest===nonce&&pending===nonce,'Account nonce changed or another transaction is pending. Reconcile before sending.');
   if(action.to===null){
    assert((await c.getCode({address:action.creates}))===undefined,'A contract already exists at the planned address');
   }
   const gas=await c.estimateGas({account:state.plan.owner,...(action.to?{to:action.to}:{}),data:action.data,value:0n});
   state.records[id]={status:'claimed',claimedAt:new Date().toISOString(),estimatedGas:String(gas),transportNonce:nonce};await persist();
   return {from:action.from,...(action.to?{to:action.to}:{}),data:action.data,value:'0x0',nonce:'0x'+nonce.toString(16),gas:'0x'+((gas*120n+99n)/100n).toString(16)};
  }),
  rejected:id=>serial(async()=>{
   const action=current(id);assert(state.records[id]?.status==='claimed','Only an unsigned claim can be rejected');
   const c=await chain(action);
   const counts=await Promise.all(['latest','pending'].map(blockTag=>c.getTransactionCount({address:state.plan.owner,blockTag})));
   assert(counts.every(n=>n===actionNonce(state,action)),'Nonce changed; recover the transaction hash');
   state.records[id]={...state.records[id],status:'rejected'};await persist();
  }),
  attach:(id,hash)=>serial(async()=>{
   const action=current(id);assert(/^0x[\da-fA-F]{64}$/.test(hash)&&BigInt(hash)!==0n,'A real nonzero transaction hash is required');
   assert(['claimed','submitted'].includes(state.records[id]?.status),'This action has no admitted signing request');
   assert(!state.records[id].hash||same(state.records[id].hash,hash),'A different transaction is already recorded');
   assert(!state.plan.actions.some(a=>a.id!==id&&a.chainId===action.chainId&&same(state.records[a.id]?.hash,hash)),'This transaction is already recorded for another action');
   const c=await chain(action),execution=await verifySetupExecution(c,{...action,nonce:actionNonce(state,action)},hash);
   state.records[id]={...state.records[id],status:'submitted',hash,execution};await persist();return state.records[id];
  }),
  reconcile:()=>serial(async()=>{
   const action=nextAction(state);if(!action)return null;
   const record=state.records[action.id];if(record?.status!=='submitted')return record||null;
   const c=await chain(action);
   let receipt;try{receipt=await c.getTransactionReceipt({hash:record.hash});}catch(e){if(e.name==='TransactionReceiptNotFoundError')return record;throw e;}
   const canonical=await c.getBlock({blockNumber:receipt.blockNumber});assert(same(canonical.hash,receipt.blockHash),'Receipt is not on the canonical chain');
   const execution=await verifySetupExecution(c,{...action,nonce:actionNonce(state,action)},record.hash);
   if(receipt.status!=='success'){record.status='failed';record.error='Transaction reverted. Inspect and reconcile; no automatic retry.';await persist();return record;}
   if(action.creates){assert(same(receipt.contractAddress,action.creates),'Created address differs from the reviewed address');assert((await c.getCode({address:action.creates}))?.length>2,'Creation receipt has no code');}
   Object.assign(record,{status:'confirmed',blockNumber:String(receipt.blockNumber),blockHash:receipt.blockHash,
    gasUsed:String(receipt.gasUsed),effectiveGasPrice:String(receipt.effectiveGasPrice),execution});await persist();return record;
  }),
 };
}
