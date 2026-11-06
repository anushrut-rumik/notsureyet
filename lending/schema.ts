import { z } from 'zod';

// Wire integers are decimal strings, so JSON never loses uint256 precision.
export const uint = z.string().regex(/^(0|[1-9][0-9]*)$/).max(78)
  .pipe(z.string().refine(value => BigInt(value) < 2n ** 256n, 'Integer exceeds uint256'));
const positive = uint.pipe(z.string().refine(value => BigInt(value) > 0n, 'Must be positive'));
const id = z.string().min(1).max(128);
const address = z.string().regex(/^0x[a-fA-F0-9]{40}$/)
  .refine(value => !/^0x0{40}$/.test(value), 'Zero wallet address');
const decimals = z.number().int().min(0).max(36);
const aprBps = uint.pipe(z.string().refine(value => BigInt(value) <= 1_000_000n, 'APR exceeds model limit'));
export const evidenceSchema = z.object({
  observedAt: uint,
  source: z.string().min(1).max(256),
  simulated: z.boolean(),
}).strict();
export const locationSchema = z.object({chainId: positive, poolId: id, tokenId: id}).strict();
const balanceSchema = z.object({
  chainId: positive, assetId: id, assetDecimals: decimals,
  availableUnits: uint, reservedUnits: uint, evidence: evidenceSchema,
}).strict();

// Each category owns a disjoint cost. An included fee is explicit zero, with a
// source explaining which other category pays it. No generic "total" line.
export const costCategories = [
  'withdrawalFee', 'sourceApprovalGas', 'destinationApprovalGas', 'sourceGas', 'bridgeFee',
  'destinationGas', 'depositFee',
] as const;
export const costSchema = z.object({
  category: z.enum(costCategories), chainId: positive, assetId: id,
  assetDecimals: decimals, amountUnits: uint, priceMicrounitsPerToken: positive,
  evidence: evidenceSchema,
}).strict();
const exitQuoteSchema = z.object({
  // Aggregate eventual withdrawal + gas cost, in fractions of one microunit.
  numeratorMicrounits: uint, denominator: positive, evidence: evidenceSchema,
}).strict();

export const policySchema = z.object({
  version: id, approvalId: id, userId: id, walletAddress: address,
  approvedAt: uint, deadline: uint,
  valuationUnit: id, allowSimulatedInputs: z.boolean(),
  approvedPools: z.array(locationSchema).min(1).max(2),
  protectedIdleReserveUnits: uint, maxMovableUnits: positive,
  minNetBenefitMicrounits: uint, maxFeeMicrounits: uint,
  maxObservationAgeSeconds: positive, maxObservationSkewSeconds: uint,
  cooldownSeconds: uint, lastMoveCompletedAt: uint.nullable(),
}).strict();

export const snapshotSchema = z.object({
  id,
  valuation: z.object({
    unit: id, decimals: z.literal(6), tokenDecimals: decimals,
    priceMicrounitsPerToken: positive, evidence: evidenceSchema,
  }).strict(),
  position: z.object({
    id, userId: id, walletAddress: address, location: locationSchema,
    investedUnits: uint, movableUnits: positive, evidence: evidenceSchema,
  }).strict(),
  reserve: z.object({
    chainId: positive, tokenId: id, idleBalanceUnits: uint,
    committedUnits: uint, evidence: evidenceSchema,
  }).strict(),
  currentPool: z.object({
    location: locationSchema, aprBps, withdrawalAvailableUnits: uint,
    evidence: evidenceSchema,
  }).strict(),
  destinationPool: z.object({
    location: locationSchema, aprBps, depositCapacityUnits: uint,
    evidence: evidenceSchema,
  }).strict(),
  costs: z.object({
    components: z.array(costSchema).length(costCategories.length),
    exitIfStay: exitQuoteSchema, exitIfMove: exitQuoteSchema,
  }).strict(),
  feeBalances: z.array(balanceSchema).max(16),
  // Sunk strategy costs are reported separately, never recharged to a move.
  accounting: z.object({
    initialDepositCostMicrounits: uint,
    realizedWithdrawalCostMicrounits: uint,
    priorRebalanceCostMicrounits: uint,
  }).strict(),
  // Null must be explicit: absence is unknown, not "no transfer".
  pendingOperation: z.object({
    operationId: id,
    status: z.enum(['prepared', 'withdrawing', 'in_transit', 'depositing', 'unknown', 'failed']),
  }).strict().nullable(),
  model: z.object({
    rates: z.literal('unchanged'), transferDowntimeSeconds: z.literal('0'),
  }).strict(),
}).strict();

export const runInputSchema = z.object({
  interfaceVersion: z.literal('1'), runId: id, networkConfigVersion: id,
  context: z.object({
    userId: id, walletAddress: address, walletBindingId: id, policyVersion: id,
  }).strict(),
  observation: z.object({snapshotId: id, assembledAt: uint, reference: id}).strict(),
  operationId: id.nullable(),
  policy: policySchema, snapshot: snapshotSchema,
}).strict();

export type DeepReadonly<T> = T extends object ? {readonly [K in keyof T]: DeepReadonly<T[K]>} : T;
export type Policy = DeepReadonly<z.infer<typeof policySchema>>;
export type MarketSnapshot = DeepReadonly<z.infer<typeof snapshotSchema>>;
export type Evidence = DeepReadonly<z.infer<typeof evidenceSchema>>;
export type RunInput = DeepReadonly<z.infer<typeof runInputSchema>>;
export type Clock = () => string; // Unix seconds, injected once per evaluation.

export function deepFreeze<T>(value: T): DeepReadonly<T> {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value as DeepReadonly<T>;
}

/** Parses into a fresh copy; caller mutations cannot change an admitted run. */
export function createRunInput(value: unknown): RunInput {
  return deepFreeze(runInputSchema.parse(value));
}

export function observations(snapshot: MarketSnapshot): readonly Evidence[] {
  return [snapshot.valuation.evidence, snapshot.position.evidence, snapshot.reserve.evidence,
    snapshot.currentPool.evidence, snapshot.destinationPool.evidence,
    ...snapshot.costs.components.map(cost => cost.evidence),
    snapshot.costs.exitIfStay.evidence, snapshot.costs.exitIfMove.evidence,
    ...snapshot.feeBalances.map(balance => balance.evidence)];
}
