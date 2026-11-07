// Part 2 receipt checks and Part 3 recovery, with per-user/per-wallet admission.
import {existsSync} from 'node:fs';
import {createPublicClient,http,decodeFunctionData,parseAbi} from 'viem';
import {testnets} from '../../.local/lending-build/testnets.js';
import {OperationJournal} from '../part3/journal.mjs';
import {confirmedReceipt,reconcile} from '../part3/monitor.mjs';
import {testnetReadTag} from '../lib/lending-confirmation.mjs';
import {verifySetupExecution} from '../lib/lending-receipts.mjs';
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const check=(ok,m)=>{if(!ok)throw Error(m);};
export async function tracking(manifest,clients) {
 const confirmation=testnetReadTag(manifest)==='latest'?'mined':'finalized';
 const getClient=id=>{const n=testnets.find(n=>n.chainId===id);check(n,'Unsupported network');
  return clients?clients(id):createPublicClient({transport:http(n.rpc,{timeout:10000,retryCount:1})});};
 const depositAbi=parseAbi(['function depositToPool(uint256)']);
 const deposits=[];
 for(const chain of manifest.chains){
  const client=getClient(chain.chainId);
  check(String(await client.getChainId())===chain.chainId,'Wrong RPC chain');
  for(const call of chain.fundingCalls??[]){
   if(!same(call.to,chain.wallet))continue;
   let decoded;try{decoded=decodeFunctionData({abi:depositAbi,data:call.data});}catch{continue;}
   check(decoded.functionName==='depositToPool'&&decoded.args[0]>0n,'Invalid deposit');
   const receipt=await confirmedReceipt(client,call.hash,confirmation);
   if(!receipt){deposits.push({chainId:chain.chainId,state:'pending',hash:call.hash});continue;}
   check(receipt.status==='success','Initial deposit reverted');
   await verifySetupExecution(client,{chainId:chain.chainId,from:manifest.owner,to:chain.wallet,data:call.data},call.hash);
   deposits.push({chainId:chain.chainId,state:'confirmed',hash:call.hash,amountUnits:String(decoded.args[0]),
    blockNumber:String(receipt.blockNumber),gasUsed:receipt.gasUsed===undefined?null:String(receipt.gasUsed),
    effectiveGasPriceWei:receipt.effectiveGasPrice===undefined?null:String(receipt.effectiveGasPrice)});
  }
 }
 const operations=[];
 if(manifest.operationJournal){
  check(existsSync(manifest.operationJournal),'Configured Part 3 journal is missing; do not create an empty replacement');
  const journal=new OperationJournal(manifest.operationJournal);
  try {
   for(const r of journal.list()) {
    if(!same(r.wallet,manifest.owner))continue;
    const observation=r.immutable.observation,context=observation.run.context;
    check(context.userId===manifest.authenticatedUserId&&same(context.walletAddress,manifest.owner),'Journal user binding mismatch');
    check(observation.chains.length===2&&observation.chains.every(c=>manifest.chains.some(m=>
     m.chainId===c.chainId&&same(m.wallet,c.wallet)&&same(m.pool,c.pool))),'Journal deployment binding mismatch');
    let current=r,monitorError=null;
    try{current=await reconcile(journal,r.id,getClient,undefined,{confirmation});}catch{monitorError='Verification pending; no retry or success inferred';}
    operations.push({id:current.id,state:current.state,sourceHash:current.details.sourceHash,
     failureReason:current.details.failureReason??null,executionError:current.details.execution?.error??null,
     destinationHash:current.details.destinationHash,messageId:current.details.messageId,
     ccipStatus:current.details.ccipStatus??null,ccipFinalityType:current.details.ccipFinalityType??null,
     ccipExecutorStatus:current.details.ccipExecutorStatus??null,
     principalUnits:current.immutable.instruction.amount,sourceChainId:current.immutable.instruction.sourceChainId,
     feeToken:{asset:'LINK',quantityUnits:current.details.actualLinkFeeUnits??null,
      maxQuotedUnits:current.immutable.instruction.maxLinkFee},inTransitSince:current.details.inTransitSince,
     verifiedDestinationPrincipal:current.details.verifiedDestinationPrincipal,
     sourceNativeGas:{asset:'ETH',chainId:current.immutable.instruction.sourceChainId,
      gasUsed:current.details.sourceGasUsed??null,effectiveGasPriceWei:current.details.sourceEffectiveGasPriceWei??null,
      paidWei:current.details.sourceGasUsed&&current.details.sourceEffectiveGasPriceWei?
       String(BigInt(current.details.sourceGasUsed)*BigInt(current.details.sourceEffectiveGasPriceWei)):null},monitorError});
   }
  } finally{journal.close();}
 }
 return {initialDeposits:deposits,operations};
}
