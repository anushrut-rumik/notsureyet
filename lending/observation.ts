import { parseAbi, encodeAbiParameters, parseAbiParameters, keccak256, toBytes, type Address, type Hex } from 'viem';
import { z } from 'zod';
import { createRunInput, runInputSchema, uint, type RunInput } from './schema.js';
import { testnets } from './testnets.js';
export const readAbi = parseAbi([
  'function observe() view returns (uint256[12])', 'function quoteLink(uint256) view returns (uint256)',
  'function decimals() view returns (uint8)', 'function binding() view returns (bytes32)',
]);
const address = z.string().regex(/^0x[0-9a-fA-F]{40}$/).refine(v => BigInt(v) !== 0n);
export const chainReadSchema = z.object({chainId: z.enum(['11155111','84532']), wallet: address,
  pool: address, blockNumber: uint, blockHash: z.string().regex(/^0x[0-9a-fA-F]{64}$/),
  binding: z.string().regex(/^0x[0-9a-fA-F]{64}$/), values: z.array(uint).length(12),
  tokenDecimals: z.literal(18), linkDecimals: z.literal(18), quoteLinkUnits: uint,
}).strict();
export const payloadSchema = z.object({scenario: z.string().min(1).max(128), run: runInputSchema, workflowId: z.string().regex(/^0x[0-9a-fA-F]{64}$/).refine(s=>BigInt(s)!==0n),
  chains: z.array(chainReadSchema).length(2),}).strict();
export type DryRunPayload = z.infer<typeof payloadSchema>;
export function rayToBps(ray: bigint): string {
  // Refuse fractional basis points; independent flooring can overstate a rate gap.
  if (ray < 0n || ray % 10n ** 23n !== 0n || ray > 1_000_000n * 10n ** 23n) throw Error('Unsupported RAY APR');
  return (ray / 10n ** 23n).toString();
}
export function validateObservedInput(payload: DryRunPayload): RunInput {
  const run = createRunInput(payload.run);
  if (new Set(payload.chains.map(c=>c.chainId)).size !== 2) throw Error('Exactly both testnets required');
  const source = payload.chains.find(c=>c.chainId===run.snapshot.position.location.chainId);
  const destination = payload.chains.find(c=>c.chainId===run.snapshot.destinationPool.location.chainId);
  if (!source || !destination || source === destination) throw Error('Wrong position chains');
  const check = (actual: unknown, expected: unknown, label: string) => {
    if(actual !== expected) throw Error(`Observed input mismatch: ${label}`);
  };
  for (const c of payload.chains) {
    const v=c.values;
    const n=testnets.find(n=>n.chainId===c.chainId)!;
    const peer=payload.chains.find(p=>p.chainId!==c.chainId)!;
    const expected=keccak256(encodeAbiParameters(parseAbiParameters('address,address,address,address,address,address,bytes32,bytes32,uint256,uint256,address'),
      [run.context.walletAddress as Address,c.pool as Address,n.token,n.link,n.router,n.forwarder,payload.workflowId as Hex,
       keccak256(toBytes(run.policy.version)),BigInt(run.policy.protectedIdleReserveUnits),BigInt(run.policy.maxMovableUnits),peer.wallet as Address]));
    check(c.binding.toLowerCase(),expected.toLowerCase(),'wallet configuration binding');
    if(BigInt(c.blockNumber)===0n || BigInt(c.blockHash)===0n) throw Error('Missing block evidence');
    check(v[0],c.chainId,'RPC chain ID'); check(v[1],c.blockNumber,'block number');
    check(BigInt(v[11]!),BigInt(run.context.walletAddress),'dedicated owner');
    if (!testnets.some(n=>n.chainId===c.chainId)) throw Error('Mainnet prohibited');
  }
  const s=run.snapshot, a=source.values, b=destination.values;
  check(s.position.investedUnits,a[4],'invested principal');
  check(s.reserve.idleBalanceUnits,a[5],'idle reserve');
  if(!s.currentPool.evidence.simulated || !s.destinationPool.evidence.simulated) throw Error('Mock APR must be labelled simulated');
  check(s.currentPool.aprBps,rayToBps(BigInt(a[3]!)),'source APR');
  check(s.destinationPool.aprBps,rayToBps(BigInt(b[3]!)),'destination APR');
  check(s.currentPool.withdrawalAvailableUnits,(BigInt(a[4]!) < BigInt(a[6]!) ? a[4] : a[6]),'withdrawal liquidity');
  check(s.destinationPool.depositCapacityUnits,b[7],'deposit capacity');
  check(s.valuation.tokenDecimals,source.tokenDecimals,'token decimals');
  // A single managed position: an existing destination position must be reconciled first.
  check(b[4],'0','destination principal must be empty');
  check(s.position.evidence.observedAt,a[2],'position observation time');
  check(s.reserve.evidence.observedAt,a[2],'reserve observation time');
  check(s.currentPool.evidence.observedAt,a[2],'source observation time');
  check(s.destinationPool.evidence.observedAt,b[2],'destination observation time');
  const bridge=s.costs.components.find(c=>c.category==='bridgeFee')!;
  check(bridge.amountUnits,source.quoteLinkUnits,'original LINK quote');
  check(bridge.assetId,'LINK','quote asset'); check(bridge.assetDecimals,18,'LINK decimals');
  check(bridge.chainId,source.chainId,'quote chain'); check(bridge.evidence.observedAt,a[2],'quote time');
  for (const c of payload.chains) {
    for (const [asset,index] of [['LINK',8],['ETH_GAS',9]] as const) {
      const balance=s.feeBalances.find(f=>f.chainId===c.chainId && f.assetId===asset);
      if (!balance) throw Error('Missing observed fee funding');
      check(balance.availableUnits,c.values[index],'fee balance'); check(balance.assetDecimals,18,'fee decimals');
      check(balance.evidence.observedAt,c.values[2],'fee funding time');
    }
  }
  return run;
}
