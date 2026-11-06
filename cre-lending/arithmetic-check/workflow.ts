import {cre,decodeJson,type Runtime,type HTTPPayload} from '@chainlink/cre-sdk';
import {z} from 'zod';
import {createRunInput,observations} from '../../lending/schema.js';
import {evaluateRun} from '../../lending/run.js';
import {resultLogLines} from '../../lending/cre-evidence.js';
export const configSchema=z.object({mode:z.literal('local-simulation'),scope:z.literal('arithmetic-only')}).strict();
type Config=z.infer<typeof configSchema>;
export function onTrigger(runtime:Runtime<Config>,trigger:HTTPPayload):string {
  if(trigger.input.length>65536)throw Error('Oversized arithmetic input');
  const input=createRunInput(decodeJson(trigger.input));
  if(!observations(input.snapshot).every(e=>e.simulated && e.source.startsWith('arithmetic-fixture:')))
    throw Error('This workflow accepts only labelled arithmetic fixtures');
  const now=Math.floor(runtime.now().getTime()/1000).toString();
  const output=JSON.stringify({mode:runtime.config.mode,scope:runtime.config.scope,
    deployedContractsRead:false,broadcast:false,result:evaluateRun(input,()=>now)});
  for(const line of resultLogLines(output))runtime.log(line);
  return output;
}
export function initWorkflow(_:Config){
  return [cre.handler(new cre.capabilities.HTTPCapability().trigger({authorizedKeys:[]}),onTrigger)];
}
