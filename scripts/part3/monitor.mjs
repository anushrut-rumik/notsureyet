import {parseAbi,decodeEventLog,encodeFunctionData} from 'viem';
import {testnets} from '../../.local/lending-build/testnets.js';
export const abi=parseAbi([
 'event SourceSubmitted(bytes32 indexed operationId,bytes32 indexed messageId,address destination,address token,uint256 amount,uint256 linkFee)',
 'event PoolDeposited(bytes32 indexed messageId,bytes32 indexed operationId,address token,uint256 amount,uint256 principalAfter)',
 'event DepositFailed(bytes32 indexed messageId,bytes32 indexed operationId,uint256 amount,bytes reason)',
 'event DepositRecovered(bytes32 indexed messageId)',
 'event Transfer(address indexed from,address indexed to,uint256 value)',
 'function sentMessages(bytes32) view returns(bytes32)',
 'function activeOperation() view returns(bytes32)',
 'function balanceOf(address) view returns(uint256)',
 'function retryDeposit(bytes32)', 'function recoverFailedDeposit(bytes32)',
 'function settleOperation(bytes32,bytes32)',
 'function getDeposit(bytes32) view returns (((bytes32 messageId,uint64 sourceChainSelector,bytes sender,bytes data,(address token,uint256 amount)[] destTokenAmounts) message,bytes reason,uint8 state,bytes32 operationId,uint256 amount))',
]);
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const requireFact=(v,m)=>{if(!v)throw Error(m);};
const hash=v=>/^0x[\da-fA-F]{64}$/.test(v??'')&&BigInt(v)!==0n;
function events(receipt,address,name){return receipt.logs.filter(l=>same(l.address,address)).flatMap(l=>{
 try{const e=decodeEventLog({abi,data:l.data,topics:l.topics,strict:true});return e.eventName===name?[e.args]:[];}catch{return [];}
});}
export async function finalizedReceipt(client,txHash){
 requireFact(hash(txHash),'Expected a real transaction hash');
 let receipt;try{receipt=await client.getTransactionReceipt({hash:txHash});}catch(e){if(e.name==='TransactionReceiptNotFoundError')return null;throw e;}
 const [canonical,finalized]=await Promise.all([client.getBlock({blockNumber:receipt.blockNumber}),client.getBlock({blockTag:'finalized'})]);
 requireFact(same(canonical.hash,receipt.blockHash),'Noncanonical receipt; preserve operation and reconcile');
 if(finalized.number<receipt.blockNumber)return null;
 return receipt;
}
export function verifySourceReceipt(record,receipt){
 const i=record.immutable.instruction;
 requireFact(receipt.status==='success','Source transaction reverted');
 requireFact(same(receipt.from,record.immutable.observation.run.context.walletAddress),'CRE broadcast account must be the dedicated faucet-funded owner');
 const matches=events(receipt,i.consumer,'SourceSubmitted').filter(e=>same(e.operationId,i.operationId));
 requireFact(matches.length===1,'Outer receipt succeeded without SourceSubmitted: inner report consumer failed or wrong transaction');
 const e=matches[0];
 requireFact(hash(e.messageId)&&same(e.destination,i.destination)&&same(e.token,i.token)&&e.amount===BigInt(i.amount)&&e.linkFee<=BigInt(i.maxLinkFee),'Source event differs from approved instruction');
 return {sourceHash:receipt.transactionHash,messageId:e.messageId,sourceBlock:String(receipt.blockNumber),
  sourceBlockHash:receipt.blockHash,actualLinkFeeUnits:String(e.linkFee),
  sourceGasUsed:receipt.gasUsed===undefined?null:String(receipt.gasUsed),
  sourceEffectiveGasPriceWei:receipt.effectiveGasPrice===undefined?null:String(receipt.effectiveGasPrice),inTransitSince:new Date().toISOString(),failureReason:null};
}
export function verifyMessage(record,message){
 const i=record.immutable.instruction,s=testnets.find(n=>n.chainId===i.sourceChainId),d=testnets.find(n=>n.chainId!==i.sourceChainId);
 requireFact(same(message.messageId,record.details.messageId)&&same(message.sender,i.consumer)&&same(message.receiver,i.destination)&&
  same(message.sendTransactionHash,record.details.sourceHash)&&message.sourceNetworkInfo?.chainSelector===s.selector&&
  message.destNetworkInfo?.chainSelector===d.selector&&message.sourceNetworkInfo?.environment==='testnet'&&message.destNetworkInfo?.environment==='testnet','CCIP message identity/route mismatch');
 requireFact(message.tokenAmounts?.length>0&&message.tokenAmounts.every(t=>same(t.sourceTokenAddress,s.token)&&same(t.destTokenAddress,d.token))&&
  message.tokenAmounts.reduce((a,t)=>a+BigInt(t.amount),0n)===BigInt(i.amount),'CCIP token/amount mismatch');
 return message;
}
export async function verifyDestination(record,receipt,client){
 const i=record.immutable.instruction,ob=record.immutable.observation,d=ob.chains.find(c=>c.chainId!==i.sourceChainId),n=testnets.find(n=>n.chainId===d.chainId);
 requireFact(receipt.status==='success','Destination transaction reverted');
 const deposited=events(receipt,d.wallet,'PoolDeposited').filter(e=>same(e.messageId,record.details.messageId)&&same(e.operationId,i.operationId));
 const failures=events(receipt,d.wallet,'DepositFailed').filter(e=>same(e.messageId,record.details.messageId)&&same(e.operationId,i.operationId));
 if(!deposited.length){
  requireFact(failures.length===1&&failures[0].amount===BigInt(i.amount),'No destination deposit or tracked idle-token event');
  const idle=await client.readContract({address:n.token,abi,functionName:'balanceOf',args:[d.wallet],blockNumber:receipt.blockNumber});
  requireFact(idle>=BigInt(i.amount),'Caught deposit failure without sufficient idle tokens');
  return {state:'idle_tokens',details:{destinationHash:receipt.transactionHash,failureReason:failures[0].reason,
   idleTokenUnits:i.amount,recoveryStatus:'retry-existing-deposit',destinationBlock:String(receipt.blockNumber)}};
 }
 requireFact(deposited.length===1&&!failures.length,'Ambiguous destination events');
 const event=deposited[0];
 requireFact(same(event.token,n.token)&&event.amount===BigInt(i.amount),'Wrong destination token/amount');
 const transfers=events(receipt,n.token,'Transfer').filter(t=>same(t.from,d.wallet)&&same(t.to,d.pool));
 requireFact(transfers.reduce((a,t)=>a+t.value,0n)===BigInt(i.amount),'Missing exact token transfer into expected lending pool');
 const principal=await client.readContract({address:d.pool,abi,functionName:'balanceOf',args:[d.wallet],blockNumber:receipt.blockNumber});
 requireFact(principal===BigInt(d.values[4])+BigInt(i.amount)&&event.principalAfter===principal,'Destination lending position increase is not verified');
 return {state:'verified_deposit',details:{destinationHash:receipt.transactionHash,destinationBlock:String(receipt.blockNumber),
  destinationBlockHash:receipt.blockHash,verifiedDestinationPrincipal:String(principal),idleTokenUnits:'0',failureReason:null,
  recoveryStatus:'verified',completedAt:new Date().toISOString()}};
}
export async function getMessage(messageId,fetcher=fetch){
 let response;try{response=await fetcher('https://api.ccip.chain.link/v2/messages/'+messageId,{signal:AbortSignal.timeout(15000)});}
 catch(e){if(['AbortError','TimeoutError','TypeError'].includes(e.name))return null;throw e;}
 if(response.status===404||response.status===429||response.status>=500)return null;
 requireFact(response.ok,'CCIP API request failed: '+response.status);return response.json();
}
async function scanEvents(client,address,event,args,fromBlock){
 const finalized=await client.getBlock({blockTag:'finalized'});let next=BigInt(fromBlock);const logs=[];
 // Public RPCs limit log ranges. Bounded scans keep monitoring responsive after long downtime.
 for(let page=0;page<4&&next<=finalized.number;page++){
  const end=next+999n<finalized.number?next+999n:finalized.number;
  logs.push(...await client.getLogs({address,event,args,fromBlock:next,toBlock:end}));next=end+1n;
  if(logs.length)break;
 }
 return {logs,nextBlock:String(next)};
}
export async function reconcile(journal,id,clients,messageReader=getMessage){
 let record=journal.get(id);requireFact(record,'Unknown operation');
 if(['prepared','cancelled','verified_deposit','recovered_idle'].includes(record.state))return record;
 const i=record.immutable.instruction,s=clients(i.sourceChainId),d=clients(i.sourceChainId==='11155111'?'84532':'11155111');
 // A hashless uncertain submission is NOT evidence of failure. Search canonical wallet events by operation ID.
 if(!record.details.sourceHash){
  const source=record.immutable.observation.chains.find(c=>c.chainId===i.sourceChainId);
  const {logs,nextBlock}=await scanEvents(s,i.consumer,abi[0],{operationId:i.operationId},record.details.sourceScanNextBlock??source.blockNumber);
  if(logs.length===0)return journal.update(id,record.state,{sourceScanNextBlock:nextBlock,failureReason:'No finalized source event yet. Submission remains unknown; do not resend.'});
  requireFact(logs.length===1,'Multiple source events for operation; inspect before continuing');
  record=journal.update(id,record.state,{sourceHash:logs[0].transactionHash});
 }
 if(['submitted','submission_unknown','failed'].includes(record.state)){
  if(record.state==='failed')return record; // A finalized failed consumer attempt needs explicit review, never automatic retry.
  const receipt=await finalizedReceipt(s,record.details.sourceHash);if(!receipt)return record;
  let details;try{details=verifySourceReceipt(record,receipt);}catch(e){return journal.update(id,'failed',{failureReason:e.message,recoveryStatus:'inspect-source-consumer'});}
  const block=await s.getBlock({blockNumber:receipt.blockNumber});
  record=journal.update(id,'source_confirmed',{...details,sourceBlockTimestamp:String(block.timestamp)});
 }
 if(record.state==='source_confirmed')record=journal.update(id,'in_transit');
 if(record.state==='idle_tokens'&&!record.details.recoveryHash){
  // Recover a lost destination retry response by its existing message ID, never by sending again.
  const start=record.details.destinationScanNextBlock??record.details.destinationBlock;
  const deposited=abi.find(e=>e.type==='event'&&e.name==='PoolDeposited'),recovered=abi.find(e=>e.type==='event'&&e.name==='DepositRecovered');
  const scans=await Promise.all([scanEvents(d,i.destination,deposited,{messageId:record.details.messageId},start),
   scanEvents(d,i.destination,recovered,{messageId:record.details.messageId},start)]);
  const logs=scans.flatMap(scan=>scan.logs);requireFact(logs.length<=1,'Conflicting destination recovery events');
  if(logs.length)record=journal.update(id,record.state,{recoveryHash:logs[0].transactionHash});
  else record=journal.update(id,record.state,{destinationScanNextBlock:scans[0].nextBlock});
 }
 // A recovered pool retry has a new destination receipt, but always the original CCIP message.
 if(record.details.recoveryHash&&['idle_tokens','destination_failed'].includes(record.state)){
  const receipt=await finalizedReceipt(d,record.details.recoveryHash);if(!receipt)return record;
  if(receipt.status!=='success')return journal.update(id,record.state,{failureReason:'Recovery transaction reverted',recoveryStatus:'retry-failed',lastFailedRecoveryHash:record.details.recoveryHash,recoveryHash:null});
  const recovered=events(receipt,i.destination,'DepositRecovered');
  if(recovered.some(e=>same(e.messageId,record.details.messageId))){
   requireFact(record.state==='idle_tokens','Recovery without idle deposit');
   const destination=record.immutable.observation.chains.find(c=>c.chainId!==i.sourceChainId),network=testnets.find(n=>n.chainId===destination.chainId);
   const transfers=events(receipt,network.token,'Transfer').filter(t=>same(t.from,i.destination)&&same(t.to,record.wallet));
   requireFact(transfers.reduce((a,t)=>a+t.value,0n)===BigInt(i.amount),'Recovery owner/token/amount mismatch');
   return journal.update(id,'recovered_idle',{recoveryStatus:'returned-to-fixed-owner',idleTokenUnits:'0'});
  }
  const outcome=await verifyDestination(record,receipt,d);
  if(record.state==='destination_failed')record=journal.update(id,'destination_executed');
  const time=await executionTime(record,receipt,d);
  return journal.update(id,outcome.state,{...outcome.details,...time});
 }
 const message=await messageReader(record.details.messageId);if(!message)return journal.update(id,record.state,{failureReason:'CCIP indexing/API pending; timeout is not failure'});
 verifyMessage(record,message);
 if(message.status==='FAILED'){
  if(record.state==='idle_tokens')return record;
  return journal.update(id,'destination_failed',{failureReason:'CCIP destination execution failed',
   recoveryStatus:message.readyForManualExecution===true?'eligible-for-existing-message-recovery':'waiting-for-recovery-eligibility'});
 }
 if(message.status!=='SUCCESS')return journal.update(id,record.state,{ccipStatus:message.status,failureReason:null});
 const receipt=await finalizedReceipt(d,message.receiptTransactionHash);if(!receipt)return record;
 if(['in_transit','destination_failed'].includes(record.state))record=journal.update(id,'destination_executed',{destinationHash:receipt.transactionHash});
 const outcome=await verifyDestination(record,receipt,d),time=await executionTime(record,receipt,d);
 return journal.update(id,outcome.state,{...outcome.details,...time});
}
async function executionTime(record,receipt,client){
 const block=await client.getBlock({blockNumber:receipt.blockNumber});
 const start=record.details.sourceBlockTimestamp,end=block.timestamp;
 return {destinationBlockTimestamp:String(end),actualTransitSeconds:start&&end!==undefined&&BigInt(end)>=BigInt(start)?String(BigInt(end)-BigInt(start)):null,
  transitModel:'Actual block timestamps recorded separately; the financial evaluator still explicitly assumes zero transfer downtime'};
}
export async function recoveryArtifact(record,client,messageReader=getMessage){
 const i=record.immutable.instruction,chain=testnets.find(n=>n.chainId!==i.sourceChainId);
 if(record.state==='idle_tokens')return actionReview(record,{kind:'retry-existing-deposit',chainId:chain.chainId,to:i.destination,
  data:encodeFunctionData({abi,functionName:'retryDeposit',args:[record.details.messageId]}),value:'0x0',messageId:record.details.messageId,
  method:'retryDeposit(bytes32)',note:'Owner-only retry; no source transfer. User signs through the existing wallet interface.'});
 requireFact(record.state==='destination_failed','There is no confirmed delivery failure');
 const message=verifyMessage(record,await messageReader(record.details.messageId));
 requireFact(message.status==='FAILED'&&message.readyForManualExecution===true,'Existing message is not currently eligible for recovery');
 return actionReview(record,{kind:'existing-message-recovery',chainId:chain.chainId,messageId:record.details.messageId,
  explorer:'https://ccip.chain.link/msg/'+record.details.messageId,
  method:'Supported CCIP Explorer recovery of the existing message',note:'Use the supported CCIP Explorer recovery for this exact message. No replacement source send.'});
}
function actionReview(record,artifact){
 const i=record.immutable.instruction,d=record.immutable.observation.chains.find(c=>c.chainId!==i.sourceChainId);
 return {heading:'Prepared on-chain action for user-run execution',network:'testnet',sourceChain:i.sourceChainId,destinationChain:d.chainId,
  route:testnets.find(n=>n.chainId===i.sourceChainId).selector+' -> '+i.destinationSelector,token:i.token,amount:i.amount,
  owner:record.immutable.observation.run.context.walletAddress,sourceContract:i.consumer,destinationContract:i.destination,
  expectedEffect:artifact.kind==='settle-verified-operation'?'Release only the source operation guard after verified deposit':
   'Recover only the existing message or its delivered tokens; no new source transfer',...artifact,
  review:'Review this carefully and execute it only from your own wallet-controlled environment.',
  signingBoundary:'This skill never signs or sends. You must sign and broadcast from your own wallet.'};
}
export function settleArtifact(record){
 requireFact(record?.state==='verified_deposit','Destination deposit must be verified before source settlement');
 return actionReview(record,{kind:'settle-verified-operation',chainId:record.immutable.instruction.sourceChainId,
  to:record.immutable.instruction.consumer,value:'0x0',method:'settleOperation(bytes32,bytes32)',
  data:encodeFunctionData({abi,functionName:'settleOperation',args:[record.id,record.details.messageId]})});
}
export async function resolveNoSend(journal,id,client){
 const r=journal.get(id);requireFact(r&&['submission_unknown','submitted','failed'].includes(r.state),'No unresolved source submission to reconcile');
 const i=r.immutable.instruction,block=await client.getBlock({blockTag:'finalized'});
 requireFact(block.timestamp>=BigInt(i.expiry),'Signed report is still executable; keep monitoring');
 const [messageId,active,latestNonce,pendingNonce]=await Promise.all([
  client.readContract({address:i.consumer,abi,functionName:'sentMessages',args:[i.operationId],blockNumber:block.number}),
  client.readContract({address:i.consumer,abi,functionName:'activeOperation',blockNumber:block.number}),
  client.getTransactionCount({address:r.wallet,blockTag:'latest'}),client.getTransactionCount({address:r.wallet,blockTag:'pending'}),
 ]);
 requireFact(BigInt(messageId)===0n&&BigInt(active)===0n,'A source operation exists; reconcile its message instead of cancelling');
 requireFact(latestNonce===pendingNonce,'Owner still has a pending transaction; reconcile nonce/transaction first');
 if(r.details.sourceHash){
  const receipt=await finalizedReceipt(client,r.details.sourceHash);requireFact(receipt,'Known source transaction is not finalized');
  requireFact(events(receipt,i.consumer,'SourceSubmitted').every(e=>!same(e.operationId,id)),'Source receipt contains a token send; cancellation rejected');
 }
 return journal.cancelAfterExpiryProof(id,{operationId:id,finalizedBlock:String(block.number),finalizedHash:block.hash,
  finalizedTimestamp:String(block.timestamp),sentMessageId:messageId,activeOperation:active,ownerHasPendingTransaction:false,latestNonce,pendingNonce,
  note:'No token transfer for this operation. A source attempt may still have spent gas.'});
}
