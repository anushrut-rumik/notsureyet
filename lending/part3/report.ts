import {encodeAbiParameters, parseAbiParameters, keccak256, toBytes, type Address, type Hex} from 'viem';
import {z} from 'zod';
import {payloadSchema, validateObservedInput} from '../observation.js';
import {uint} from '../schema.js';
import {testnets} from '../testnets.js';

const hash = z.string().regex(/^0x[0-9a-fA-F]{64}$/).refine(s=>BigInt(s)!==0n);
const address = z.string().regex(/^0x[0-9a-fA-F]{40}$/).refine(s=>BigInt(s)!==0n);
export const instructionSchema = z.object({operationId:hash, sourceChainId:uint, consumer:address,
  destinationSelector:uint, destination:address, token:address, amount:uint, policyVersion:hash,
  expiry:uint, maxLinkFee:uint, maxGasPrice:uint, sourceRateRay:uint, observedAt:uint, inputHash:hash}).strict();
export const executionPayloadSchema = z.object({observation:payloadSchema, instruction:instructionSchema,
  signature:z.string().regex(/^0x[0-9a-fA-F]{130}$/)}).strict();
export type ExecutionPayload = z.infer<typeof executionPayloadSchema>;
export const instructionTypes = {Instruction:[
  ['operationId','bytes32'],['sourceChainId','uint256'],['consumer','address'],['destinationSelector','uint64'],
  ['destination','address'],['token','address'],['amount','uint256'],['policyVersion','bytes32'],['expiry','uint256'],
  ['maxLinkFee','uint256'],['maxGasPrice','uint256'],['sourceRateRay','uint256'],['observedAt','uint256'],['inputHash','bytes32'],
].map(([name,type])=>({name:name!,type:type!}))};
export const reportParameters = parseAbiParameters('(bytes32 operationId,uint256 sourceChainId,address consumer,uint64 destinationSelector,address destination,address token,uint256 amount,bytes32 policyVersion,uint256 expiry,uint256 maxLinkFee,uint256 maxGasPrice,uint256 sourceRateRay,uint256 observedAt,bytes32 inputHash) instruction, bytes signature');

// Stable object ordering makes the owner's signature cover the entire decision input.
export function canonicalJson(value:unknown): string {
  if(Array.isArray(value))return '['+value.map(canonicalJson).join(',')+']';
  if(value!==null&&typeof value==='object')return '{'+Object.entries(value).sort(([a],[b])=>a<b?-1:a>b?1:0)
    .map(([k,v])=>JSON.stringify(k)+':'+canonicalJson(v)).join(',')+'}';
  return JSON.stringify(value);
}
export const inputHash=(value:unknown)=>keccak256(toBytes(canonicalJson(value)));
export function buildInstruction(observation:z.infer<typeof payloadSchema>, expiry:string) {
  const run=validateObservedInput(observation),s=observation.chains.find(c=>c.chainId===run.snapshot.position.location.chainId)!;
  const d=observation.chains.find(c=>c.chainId!==s.chainId)!,n=testnets.find(n=>n.chainId===s.chainId)!;
  if(!run.operationId)throw Error('An operation ID is required');
  if(BigInt(expiry)>BigInt(run.policy.deadline)||BigInt(expiry)>BigInt(s.values[2]!)+1800n)throw Error('Instruction expiry exceeds approval or freshness bound');
  return instructionSchema.parse({operationId:keccak256(toBytes(run.operationId)),sourceChainId:s.chainId,consumer:s.wallet,
    destinationSelector:testnets.find(n=>n.chainId===d.chainId)!.selector,destination:d.wallet,token:n.token,
    amount:run.snapshot.position.movableUnits,policyVersion:keccak256(toBytes(run.policy.version)),expiry,
    maxLinkFee:s.quoteLinkUnits,maxGasPrice:String(BigInt(run.snapshot.costs.components.find(c=>c.category==='sourceGas')!.amountUnits)/1500000n),
    sourceRateRay:s.values[3],observedAt:s.values[2],inputHash:inputHash(observation)});
}
export function validateExecutionPayload(value:unknown): ExecutionPayload {
  const p=executionPayloadSchema.parse(value),expected=buildInstruction(p.observation,p.instruction.expiry);
  if(canonicalJson(expected)!==canonicalJson(p.instruction))throw Error('Signed instruction differs from the decision input');
  const costs=p.observation.run.snapshot.costs.components;
  for(const category of ['withdrawalFee','sourceApprovalGas','destinationApprovalGas','destinationGas','depositFee'])
    if(costs.find(c=>c.category===category)?.amountUnits!=='0')throw Error('Bundled or zero pool cost counted twice: '+category);
  const gas=costs.find(c=>c.category==='sourceGas')!;
  if(gas.assetId!=='ETH_GAS'||gas.chainId!==p.instruction.sourceChainId||gas.amountUnits==='0')throw Error('Separate nonzero source gas budget required');
  if(p.observation.run.snapshot.position.investedUnits!==p.instruction.amount)throw Error('Part 3 moves the entire managed position');
  return p;
}
export function typedInstruction(i:z.infer<typeof instructionSchema>) {
  return Object.fromEntries(Object.entries(i).map(([k,v])=>[k,['sourceChainId','destinationSelector','amount','expiry','maxLinkFee','maxGasPrice','sourceRateRay','observedAt'].includes(k)?BigInt(v):v]));
}
export function signingRequest(p: Pick<ExecutionPayload,'instruction'>) {
  return {domain:{name:'LendingExecutionInstruction',version:'1',chainId:Number(p.instruction.sourceChainId),verifyingContract:p.instruction.consumer as Address},
    types:instructionTypes,primaryType:'Instruction' as const,message:typedInstruction(p.instruction)};
}
export function encodeExecutionReport(p:ExecutionPayload): Hex {
  return encodeAbiParameters(reportParameters,[typedInstruction(p.instruction) as never,p.signature as Hex]);
}
