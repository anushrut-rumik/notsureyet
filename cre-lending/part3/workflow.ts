import {cre,decodeJson,bytesToHex,encodeCallMsg,LATEST_BLOCK_NUMBER,TxStatus,prepareReportRequest,
  type Runtime,type HTTPPayload} from '@chainlink/cre-sdk';
import {decodeFunctionResult,encodeFunctionData,multicall3Abi,parseAbi,zeroAddress,type Address,type Hex} from 'viem';
import {z} from 'zod';
import {onTrigger as checkAnchoredObservation} from '../workflow/workflow.js';
import {validateExecutionPayload,encodeExecutionReport,instructionSchema} from '../../lending/part3/report.js';
import {testnets} from '../../lending/testnets.js';
import {readAbi} from '../../lending/observation.js';
import {evaluateRun} from '../../lending/run.js';

export const configSchema=z.discriminatedUnion('mode',[
  z.object({mode:z.enum(['testnet-execution','testnet-preflight']),configVersion:z.literal('part3-testnets-v1')}).strict(),
  z.object({mode:z.literal('local-simulation'),scope:z.literal('receiver-free-no-execution')}).strict(),
  z.object({mode:z.literal('testnet-report-preview'),scope:z.literal('metadata-only-no-write')}).strict(),
]);
type Config=z.infer<typeof configSchema>;
export const executionAbi=parseAbi(['function quoteOperation(bytes32,uint256) view returns(uint256)',
  'function activeOperation() view returns(bytes32)','function consumed(bytes32) view returns(bool)']);
// Canonical Multicall3 deployment on both supported testnets (also in viem's chain definitions).
// Only view methods are batched. A failed subcall rejects the entire observation.
export const multicall3Address='0xca11bde05977b3631167028862be2a173976ca11' as const;
export function onTrigger(runtime:Runtime<Config>,trigger:HTTPPayload):string {
  // No EVM clients, reports or writes in this branch, even if --broadcast is appended.
  if(runtime.config.mode==='local-simulation')return JSON.stringify({mode:runtime.config.mode,broadcast:false,
    executed:false,note:'Startup check only; no decision, contracts or delivery verified'});
  if(runtime.config.mode==='testnet-report-preview'){
    if(trigger.input.length>65536)throw Error('Oversized preview fixture');
    const data=decodeJson(trigger.input) as Record<string,unknown>;
    let body:Hex='0x00';
    if('instruction' in data){
      const fixture=z.object({kind:z.literal('Independent public arithmetic/encoding fixture, not a signed live action'),
        instruction:instructionSchema,signature:z.string().regex(/^0x[0-9a-fA-F]{130}$/),
        digest:z.string().regex(/^0x[0-9a-fA-F]{64}$/),raw:z.string().regex(/^0x[0-9a-fA-F]+$/)}).strict().parse(data);
      body=encodeExecutionReport(fixture as never);
      if(body!==fixture.raw)throw Error('Native report ABI differs from independent golden vector');
    }else z.object({}).strict().parse(data);
    const report=runtime.report(prepareReportRequest(body)).result();
    if(bytesToHex(report.body())!==body)throw Error('Native report body differs from encoded instruction');
    return JSON.stringify({mode:runtime.config.mode,executed:false,workflowId:'0x'+report.workflowId(),
      workflowName:report.workflowName(),workflowOwner:'0x'+report.workflowOwner(),reportBodyMatchesABI:true});
  }
  if(trigger.input.length>65536)throw Error('Oversized execution payload');
  const p=validateExecutionPayload(decodeJson(trigger.input));
  // This execution target is restricted to Sepolia and Base Sepolia. Verify the
  // exact canonical snapshot without waiting for a finalized checkpoint.
  checkAnchoredObservation(runtime as never,{...trigger,input:new TextEncoder().encode(JSON.stringify(p.observation))},false);
  const i=p.instruction;
  const requests=p.observation.chains.map(c=>{
    const n=testnets.find(n=>n.chainId===c.chainId)!;
    const client=new cre.capabilities.EVMClient(BigInt(n.selector));
    const call=(abi:typeof readAbi|typeof executionAbi,name:string,args?:readonly unknown[])=>({
      target:c.wallet as Address,allowFailure:false,callData:encodeFunctionData({abi,functionName:name,args} as never)});
    const calls=[call(readAbi,'observe'),call(readAbi,'binding'),call(executionAbi,'activeOperation'),
      call(executionAbi,'consumed',[i.operationId as Hex])];
    if(c.chainId===i.sourceChainId)calls.push(call(executionAbi,'quoteOperation',[i.operationId as Hex,BigInt(i.amount)]));
    // The anchored checks use 12 reads including headers. Two batches keep the
    // complete execution at 14, within CRE's default 15-read limit, with no checks removed.
    return {c,client,count:calls.length,state:client.callContract(runtime,{
      call:encodeCallMsg({from:zeroAddress,to:multicall3Address,
        data:encodeFunctionData({abi:multicall3Abi,functionName:'aggregate3',args:[calls]})}),
      blockNumber:LATEST_BLOCK_NUMBER})};
  });
  let source:typeof requests[number]|undefined;
  for(const r of requests){
    const batch=decodeFunctionResult({abi:multicall3Abi,functionName:'aggregate3',data:bytesToHex(r.state.result().data)});
    if(batch.length!==r.count||batch.some(item=>!item.success))throw Error('Incomplete current-state contract reads');
    const decode=(abi:typeof readAbi|typeof executionAbi,name:string,index:number)=>decodeFunctionResult({abi,functionName:name,data:batch[index]!.returnData} as never);
    const values=decode(readAbi,'observe',0) as readonly bigint[];
    // Blocks can advance; economically relevant state must still match the signed observation.
    for(const index of [0,3,4,5,6,7,8,9,10,11])if(String(values[index])!==r.c.values[index])throw Error('Current balance/rate/capacity changed; cancel and prepare fresh input');
    if(decode(readAbi,'binding',1)!==r.c.binding)throw Error('Policy or wallet binding changed');
    if(BigInt(String(decode(executionAbi,'activeOperation',2)))!==0n ||
      decode(executionAbi,'consumed',3)!==false)throw Error('An operation is already pending or consumed');
    if(r.c.chainId===i.sourceChainId){source=r;if(String(decode(executionAbi,'quoteOperation',4))!==i.maxLinkFee)throw Error('Actual router fee changed');}
  }
  const now=Math.floor(runtime.now().getTime()/1000).toString();
  const result=evaluateRun(p.observation.run,()=>now);
  if(result.decision.action!=='move')return JSON.stringify({submitted:false,result});
  if(BigInt(now)>=BigInt(i.expiry))throw Error('Signed instruction expired');
  const report=runtime.report(prepareReportRequest(encodeExecutionReport(p))).result();
  // This target uses verified testnet MockKeystoneForwarders. Their metadata is NOT
  // authenticated workflow identity; the consumer requires the bound owner EIP-712 signature.
  if(report.workflowName().replace(/\0+$/,'')!=='eb6597b43f')throw Error('Unexpected encoded CRE workflow name');
  if(runtime.config.mode==='testnet-preflight'){
    const checked={preflight:true,validated:true,operationId:i.operationId};
    runtime.log('PART3_EXECUTION_RESULT='+JSON.stringify(checked));
    return JSON.stringify(checked); // No chain-write capability in the preflight target.
  }
  const written=source!.client.writeReport(runtime,{receiver:i.consumer,report,gasConfig:{gasLimit:'1500000'}}).result();
  // Preserve even a reverted transaction's hash before raising. It still needs
  // receipt reconciliation and must not turn into a hashless unknown attempt.
  if(written.txHash?.length===32&&written.txHash.some(b=>b!==0)){
    runtime.log('PART3_EXECUTION_RESULT='+JSON.stringify({submitted:true,
      sourceTransactionHash:bytesToHex(written.txHash),operationId:i.operationId,transactionStatus:written.txStatus}));
  }
  if(written.txStatus!==TxStatus.SUCCESS)throw Error('Submission outcome requires reconciliation: '+(written.errorMessage??written.txStatus));
  if(!written.txHash||written.txHash.length!==32||written.txHash.every(b=>b===0))throw Error('Unknown submission outcome: missing source hash');
  return JSON.stringify({submitted:true,state:'submitted',sourceTransactionHash:bytesToHex(written.txHash),
    operationId:i.operationId,result,note:'Source consumer event and destination deposit remain unverified'});
}
export function initWorkflow(_:Config){return [cre.handler(new cre.capabilities.HTTPCapability().trigger({authorizedKeys:[]}),onTrigger)];}
