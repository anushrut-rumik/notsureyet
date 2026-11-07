import { cre, decodeJson, bytesToHex, encodeCallMsg, bigintToProtoBigInt,
  protoBigIntToBigint, LAST_FINALIZED_BLOCK_NUMBER, type Runtime, type HTTPPayload } from '@chainlink/cre-sdk';
import { decodeFunctionResult, encodeFunctionData, zeroAddress, type Address } from 'viem';
import { z } from 'zod';
import { payloadSchema, readAbi, validateObservedInput } from '../../lending/observation.js';
import { testnets } from '../../lending/testnets.js';
import { resultLogLines } from '../../lending/cre-evidence.js';
import { evaluateRun } from '../../lending/run.js';
export const configSchema = z.object({mode:z.enum(['testnet-read-only','testnet-mined-read-only']),configVersion:z.literal('part2-testnets-v1')}).strict();
type Config=z.infer<typeof configSchema>;
export function onTrigger(runtime: Runtime<Config>, trigger: HTTPPayload, requireFinality=runtime.config.mode!=='testnet-mined-read-only'): string {
  // Limit before parsing/validation. No HTTP client, report creation or write capability is used.
  if(trigger.input.length > 65536) throw Error('Oversized immutable run payload');
  const payload=payloadSchema.parse(decodeJson(trigger.input));
  const now=Math.floor(runtime.now().getTime()/1000).toString();
  validateObservedInput(payload);
  const requests=payload.chains.map(c=>{
    const network=testnets.find(n=>n.chainId===c.chainId)!;
    const client=new cre.capabilities.EVMClient(BigInt(network.selector));
    const blockNumber=bigintToProtoBigInt(c.blockNumber);
    const call=(address: string, functionName:'observe'|'quoteLink'|'decimals'|'binding') => client.callContract(runtime,{
      call:encodeCallMsg({from:zeroAddress,to:address as Address,data:encodeFunctionData({abi:readAbi,functionName,
        args:functionName==='quoteLink' ? [BigInt(payload.run.snapshot.position.movableUnits)] : undefined})}),blockNumber});
    // Start EVERY independent capability before resolving any of them.
    return {c, header:client.headerByNumber(runtime,{blockNumber}),
      finalized:requireFinality?client.headerByNumber(runtime,{blockNumber:LAST_FINALIZED_BLOCK_NUMBER}):null,
      state:call(c.wallet,'observe'), binding:call(c.wallet,'binding'),
      tokenDecimals:call(network.token,'decimals'), linkDecimals:call(network.link,'decimals'),
      quote:call(c.wallet,'quoteLink')};
  });
  for(const r of requests){
    const header=r.header.result().header, finalized=r.finalized?.result().header;
    if(!header?.blockNumber || (requireFinality&&!finalized?.blockNumber) || protoBigIntToBigint(header.blockNumber)!==BigInt(r.c.blockNumber) || bytesToHex(header.hash).toLowerCase()!==r.c.blockHash.toLowerCase() ||
      (finalized?.blockNumber&&protoBigIntToBigint(finalized.blockNumber)<BigInt(r.c.blockNumber)) || header.timestamp.toString()!==r.c.values[2])
      throw Error('Block hash, timestamp or finality mismatch');
    const decode=(data:Uint8Array,functionName:'observe'|'quoteLink'|'decimals'|'binding')=>decodeFunctionResult({abi:readAbi,functionName,data:bytesToHex(data)});
    const values=decode(r.state.result().data,'observe') as readonly bigint[];
    if(JSON.stringify(values.map(String))!==JSON.stringify(r.c.values) ||
      decode(r.binding.result().data,'binding')!==r.c.binding ||
      decode(r.tokenDecimals.result().data,'decimals')!==r.c.tokenDecimals ||
      decode(r.linkDecimals.result().data,'decimals')!==r.c.linkDecimals ||
      String(decode(r.quote.result().data,'quoteLink'))!==r.c.quoteLinkUnits)
      throw Error('Immutable snapshot does not match deployed contracts');
  }
  const run=validateObservedInput(payload);
  const result={mode:runtime.config.mode,scenario:payload.scenario,
    limitations:['Mock rate does not accrue interest','Fictional valuations and gas estimates remain labelled','Recommendation only'],
    chains:payload.chains,result:evaluateRun(run,()=>now)};
  const output=JSON.stringify(result);
  for (const line of resultLogLines(output)) runtime.log(line);
  return output;
}
export function initWorkflow(_: Config) {
  return [cre.handler(new cre.capabilities.HTTPCapability().trigger({authorizedKeys:[]}),onTrigger)];
}
