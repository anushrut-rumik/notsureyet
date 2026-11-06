import {cre,decodeJson,bytesToHex,encodeCallMsg,LAST_FINALIZED_BLOCK_NUMBER,TxStatus,prepareReportRequest,
  type Runtime,type HTTPPayload} from '@chainlink/cre-sdk';
import {decodeFunctionResult,encodeFunctionData,parseAbi,zeroAddress,type Address,type Hex} from 'viem';
import {z} from 'zod';
import {onTrigger as checkAnchoredObservation} from '../workflow/workflow.js';
import {validateExecutionPayload,encodeExecutionReport,instructionSchema} from '../../lending/part3/report.js';
import {testnets} from '../../lending/testnets.js';
import {readAbi} from '../../lending/observation.js';
import {evaluateRun} from '../../lending/run.js';

export const configSchema=z.discriminatedUnion('mode',[
  z.object({mode:z.literal('testnet-execution'),configVersion:z.literal('part3-testnets-v1')}).strict(),
  z.object({mode:z.literal('local-simulation'),scope:z.literal('receiver-free-no-execution')}).strict(),
  z.object({mode:z.literal('testnet-report-preview'),scope:z.literal('metadata-only-no-write')}).strict(),
]);
type Config=z.infer<typeof configSchema>;
export const executionAbi=parseAbi(['function quoteOperation(bytes32,uint256) view returns(uint256)',
  'function activeOperation() view returns(bytes32)','function consumed(bytes32) view returns(bool)']);
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
  checkAnchoredObservation(runtime as never,{...trigger,input:new TextEncoder().encode(JSON.stringify(p.observation))});
  const i=p.instruction;
  const requests=p.observation.chains.map(c=>{
    const n=testnets.find(n=>n.chainId===c.chainId)!;
    const client=new cre.capabilities.EVMClient(BigInt(n.selector));
    const call=(abi:typeof readAbi|typeof executionAbi,name:string,args?:readonly unknown[])=>client.callContract(runtime,{
      call:encodeCallMsg({from:zeroAddress,to:c.wallet as Address,data:encodeFunctionData({abi,functionName:name,args} as never)}),
      blockNumber:LAST_FINALIZED_BLOCK_NUMBER});
    // Initiate all independent calls before resolving any result.
    return {c,client,state:call(readAbi,'observe'),binding:call(readAbi,'binding'),active:call(executionAbi,'activeOperation'),
      consumed:call(executionAbi,'consumed',[i.operationId as Hex]),
      quote:c.chainId===i.sourceChainId?call(executionAbi,'quoteOperation',[i.operationId as Hex,BigInt(i.amount)]):null};
  });
  let source:typeof requests[number]|undefined;
  for(const r of requests){
    const decode=(abi:typeof readAbi|typeof executionAbi,name:string,data:Uint8Array)=>decodeFunctionResult({abi,functionName:name,data:bytesToHex(data)} as never);
    const values=decode(readAbi,'observe',r.state.result().data) as readonly bigint[];
    // Blocks can advance; economically relevant state must still match the signed observation.
    for(const index of [0,3,4,5,6,7,8,9,10,11])if(String(values[index])!==r.c.values[index])throw Error('Current balance/rate/capacity changed; cancel and prepare fresh input');
    if(decode(readAbi,'binding',r.binding.result().data)!==r.c.binding)throw Error('Policy or wallet binding changed');
    if(BigInt(String(decode(executionAbi,'activeOperation',r.active.result().data)))!==0n ||
      decode(executionAbi,'consumed',r.consumed.result().data)!==false)throw Error('An operation is already pending or consumed');
    if(r.quote){source=r;if(String(decode(executionAbi,'quoteOperation',r.quote.result().data))!==i.maxLinkFee)throw Error('Actual router fee changed');}
  }
  const now=Math.floor(runtime.now().getTime()/1000).toString();
  const result=evaluateRun(p.observation.run,()=>now);
  if(result.decision.action!=='move')return JSON.stringify({submitted:false,result});
  if(BigInt(now)>=BigInt(i.expiry))throw Error('Signed instruction expired');
  const report=runtime.report(prepareReportRequest(encodeExecutionReport(p))).result();
  // This target uses verified testnet MockKeystoneForwarders. Their metadata is NOT
  // authenticated workflow identity; the consumer requires the bound owner EIP-712 signature.
  if(report.workflowName().replace(/\0+$/,'')!=='eb6597b43f')throw Error('Unexpected encoded CRE workflow name');
  const written=source!.client.writeReport(runtime,{receiver:i.consumer,report,gasConfig:{gasLimit:'1500000'}}).result();
  if(written.txStatus!==TxStatus.SUCCESS)throw Error('Submission outcome requires reconciliation: '+(written.errorMessage??written.txStatus));
  if(!written.txHash||written.txHash.length!==32||written.txHash.every(b=>b===0))throw Error('Unknown submission outcome: missing source hash');
  return JSON.stringify({submitted:true,state:'submitted',sourceTransactionHash:bytesToHex(written.txHash),
    operationId:i.operationId,result,note:'Source consumer event and destination deposit remain unverified'});
}
export function initWorkflow(_:Config){return [cre.handler(new cre.capabilities.HTTPCapability().trigger({authorizedKeys:[]}),onTrigger)];}
