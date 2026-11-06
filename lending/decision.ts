import {
  costCategories, observations, policySchema, snapshotSchema, uint,
  type Clock, type MarketSnapshot, type Policy,
} from './schema.js';
import { networkConfig } from './networks.js';

export const YEAR_SECONDS = 365n * 24n * 60n * 60n;
const BPS = 10_000n;
const ceil = (n: bigint, d: bigint) => (n + d - 1n) / d;
const floorSigned = (n: bigint, d: bigint) => n >= 0n ? n / d : -ceil(-n, d);
const ceilSigned = (n: bigint, d: bigint) => n >= 0n ? ceil(n, d) : -((-n) / d);

export interface Calculation {
  readonly valuationUnit: string;
  readonly valuationDecimals: 6;
  readonly remainingSeconds: string;
  readonly yearSeconds: string;
  readonly movableUnits: string;
  readonly protectedIdleReserveUnits: string;
  readonly aprDeltaBps: string;
  readonly interestNumerator: string;
  readonly interestDenominator: string;
  readonly additionalInterestMicrounits: string;
  readonly costBreakdown: readonly {readonly category: string; readonly costMicrounits: string}[];
  readonly upfrontCostMicrounits: string;
  readonly exitCostDifferenceMicrounits: string;
  readonly incrementalCostMicrounits: string;
  readonly feeLimitChargeMicrounits: string;
  readonly netImprovementMicrounits: string;
  readonly strategyAccounting: {
    readonly initialDepositCostMicrounits: string;
    readonly realizedWithdrawalCostMicrounits: string;
    readonly priorRebalanceCostMicrounits: string;
    readonly estimatedExitIfStayMicrounits: string;
    readonly estimatedExitIfMoveMicrounits: string;
  };
  readonly estimate: true;
  readonly simulated: boolean;
  readonly assumptions: readonly string[];
}
export interface Decision {
  readonly action: 'move' | 'hold' | 'blocked';
  readonly code: string;
  readonly reason: string;
  readonly evaluatedAt: string | null;
  readonly calculation: Calculation | null;
  readonly issues: readonly string[];
}

type Location = MarketSnapshot['position']['location'];
export const sameLocation = (a: Location, b: Location) =>
  a.chainId === b.chainId && a.poolId === b.poolId && a.tokenId === b.tokenId;

/** No I/O, global time, wallet, model, or Node APIs. The only financial rules. */
export function decide(policyInput: Policy, snapshotInput: MarketSnapshot, clock: Clock): Decision {
  let evaluatedAt: string | null = null;
  const result = (action: Decision['action'], code: string, reason: string,
    calculation: Calculation | null = null, issues: readonly string[] = []): Decision =>
    ({action, code, reason, evaluatedAt, calculation, issues});

  try {
    const time = uint.safeParse(clock());
    if (!time.success) return result('blocked', 'INVALID_CLOCK', 'Clock must return integer Unix seconds.');
    evaluatedAt = time.data;
  } catch {
    return result('blocked', 'INVALID_CLOCK', 'The injected clock failed.');
  }
  const parsedPolicy = policySchema.safeParse(policyInput);
  const parsedSnapshot = snapshotSchema.safeParse(snapshotInput);
  if (!parsedPolicy.success || !parsedSnapshot.success) {
    const issues = [
      ...(!parsedPolicy.success ? parsedPolicy.error.issues.map(i => `policy.${i.path.join('.')}: ${i.message}`) : []),
      ...(!parsedSnapshot.success ? parsedSnapshot.error.issues.map(i => `snapshot.${i.path.join('.')}: ${i.message}`) : []),
    ];
    return result('blocked', 'INVALID_INPUT', 'Required data is missing or invalid.', null, issues);
  }
  const p = parsedPolicy.data, s = parsedSnapshot.data;
  const now = BigInt(evaluatedAt), amount = BigInt(s.position.movableUnits);
  const source = s.currentPool.location, destination = s.destinationPool.location;
  const blocked = (code: string, reason: string, calculation: Calculation | null = null) =>
    result('blocked', code, reason, calculation);

  if (p.userId !== s.position.userId || p.walletAddress.toLowerCase() !== s.position.walletAddress.toLowerCase())
    return blocked('OWNER_MISMATCH', 'The position does not belong to the approved user and wallet.');
  if (BigInt(p.approvedAt) > now || BigInt(p.deadline) <= BigInt(p.approvedAt))
    return blocked('INVALID_APPROVAL_WINDOW', 'Approval must precede the deadline and cannot be in the future.');
  if (s.valuation.unit !== p.valuationUnit)
    return blocked('VALUATION_UNIT_MISMATCH', 'The snapshot and approved policy use different valuation units.');
  if (!sameLocation(source, s.position.location) || source.tokenId !== destination.tokenId)
    return blocked('POSITION_MISMATCH', 'The source must match the invested position; token changes are unsupported.');
  const locations = [source, destination, ...p.approvedPools];
  if (locations.some(location => !networkConfig.networks.some(slot => sameLocation(slot, location))))
    return blocked('UNSUPPORTED_LOCATION', 'Only the two configured testnet pool/token slots are supported.');
  if (![source, destination].every(location => p.approvedPools.some(approved => sameLocation(approved, location))))
    return blocked('UNAPPROVED_LOCATION', 'The user must approve both source and destination chain, pool and token.');
  if (s.reserve.chainId !== source.chainId || s.reserve.tokenId !== source.tokenId)
    return blocked('RESERVE_LOCATION_MISMATCH', 'The idle reserve must be available on the source chain in the managed token.');

  const evidence = observations(s);
  if (!p.allowSimulatedInputs && evidence.some(e => e.simulated))
    return blocked('SIMULATED_INPUT_DISALLOWED', 'This policy does not approve simulated observations.');
  let oldest = now, newest = 0n;
  for (const e of evidence) {
    const observed = BigInt(e.observedAt);
    if (observed > now) return blocked('FUTURE_OBSERVATION', `Observation from ${e.source} is in the future.`);
    if (now - observed > BigInt(p.maxObservationAgeSeconds))
      return blocked('STALE_OBSERVATION', `Observation from ${e.source} is too old.`);
    if (observed < oldest) oldest = observed;
    if (observed > newest) newest = observed;
  }
  if (newest - oldest > BigInt(p.maxObservationSkewSeconds))
    return blocked('OBSERVATION_SKEW', 'The observations are too far apart to compare safely.');
  if (s.pendingOperation !== null)
    return blocked('PENDING_OPERATION', `Resolve operation ${s.pendingOperation.operationId} before another move.`);
  if (p.lastMoveCompletedAt !== null && BigInt(p.lastMoveCompletedAt) > now)
    return blocked('INVALID_LAST_MOVE', 'The previous move completion time is in the future.');
  if (amount > BigInt(s.position.investedUnits) || amount > BigInt(p.maxMovableUnits))
    return blocked('MOVABLE_LIMIT', 'The amount exceeds the invested position or the approved move limit.');
  const reserveRequired = BigInt(p.protectedIdleReserveUnits) + BigInt(s.reserve.committedUnits);
  if (BigInt(s.reserve.idleBalanceUnits) < reserveRequired)
    return blocked('RESERVE_SHORTFALL', 'The protected reserve must remain idle after existing commitments.');
  if (BigInt(s.currentPool.withdrawalAvailableUnits) < amount)
    return blocked('SOURCE_LIQUIDITY', 'The source pool cannot currently release the movable amount.');
  if (BigInt(s.destinationPool.depositCapacityUnits) < amount)
    return blocked('DESTINATION_CAPACITY', 'The destination cannot accept the full movable amount.');
  if (new Set(s.costs.components.map(c => c.category)).size !== costCategories.length)
    return blocked('COST_CATEGORIES', 'Every incremental cost category must appear exactly once, including explicit zeros.');

  const balances = new Map<string, typeof s.feeBalances[number]>();
  for (const b of s.feeBalances) {
    const key = `${b.chainId}:${b.assetId}`;
    if (balances.has(key) || BigInt(b.reservedUnits) > BigInt(b.availableUnits))
      return blocked('INVALID_FEE_BALANCE', 'Fee balances must be unique and reservations cannot exceed available units.');
    if (!networkConfig.networks.some(n => n.chainId === b.chainId))
      return blocked('UNSUPPORTED_FEE_CHAIN', 'Fee balances must be on an approved testnet.');
    if (b.chainId === s.reserve.chainId && b.assetId === s.reserve.tokenId &&
      (b.availableUnits !== s.reserve.idleBalanceUnits || b.assetDecimals !== s.valuation.tokenDecimals ||
        BigInt(b.reservedUnits) < reserveRequired))
      return blocked('RESERVE_FEE_BALANCE_MISMATCH', 'Managed-token fee funding must preserve the same idle reserve and commitments.');
    balances.set(key, b);
  }
  const needed = new Map<string, bigint>();
  const prices = new Map<string, string>();
  for (const c of s.costs.components) {
    if (!networkConfig.networks.some(n => n.chainId === c.chainId))
      return blocked('UNSUPPORTED_FEE_CHAIN', 'Cost quotes must refer to an approved testnet.');
    const key = `${c.chainId}:${c.assetId}`;
    const price = `${c.assetDecimals}:${c.priceMicrounitsPerToken}`;
    if (prices.has(key) && prices.get(key) !== price)
      return blocked('FEE_VALUATION_MISMATCH', 'Quotes for the same fee asset must use the same decimals and price.');
    prices.set(key, price);
    if (c.assetId === source.tokenId && (c.assetDecimals !== s.valuation.tokenDecimals ||
      c.priceMicrounitsPerToken !== s.valuation.priceMicrounitsPerToken))
      return blocked('FEE_VALUATION_MISMATCH', 'Managed-token fees must use the position token valuation.');
    if (BigInt(c.amountUnits) === 0n) continue;
    const b = balances.get(key);
    if (!b || b.assetDecimals !== c.assetDecimals)
      return blocked('MISSING_FEE_BALANCE', `A matching fee balance is required for ${key}.`);
    needed.set(key, (needed.get(key) ?? 0n) + BigInt(c.amountUnits));
  }
  for (const [key, required] of needed) {
    const b = balances.get(key)!;
    if (required > BigInt(b.availableUnits) - BigInt(b.reservedUnits))
      return blocked('INSUFFICIENT_FEE_BALANCE', `Unreserved fee balance is insufficient for ${key}.`);
  }

  const remaining = BigInt(p.deadline) > now ? BigInt(p.deadline) - now : 0n;
  const delta = BigInt(s.destinationPool.aprBps) - BigInt(s.currentPool.aprBps);
  const numerator = amount * BigInt(s.valuation.priceMicrounitsPerToken) * delta * remaining;
  const denominator = 10n ** BigInt(s.valuation.tokenDecimals) * BPS * YEAR_SECONDS;
  const interest = floorSigned(numerator, denominator);
  const costBreakdown = s.costs.components.map(c => ({category: c.category,
    costMicrounits: ceil(BigInt(c.amountUnits) * BigInt(c.priceMicrounitsPerToken),
      10n ** BigInt(c.assetDecimals)).toString()}));
  const upfront = costBreakdown.reduce((sum, c) => sum + BigInt(c.costMicrounits), 0n);
  const exitStay = BigInt(s.costs.exitIfStay.numeratorMicrounits) / BigInt(s.costs.exitIfStay.denominator);
  const exitMove = ceil(BigInt(s.costs.exitIfMove.numeratorMicrounits), BigInt(s.costs.exitIfMove.denominator));
  // Subtract exact quotes BEFORE rounding. Equal common fractional costs cancel.
  const stayQuote = s.costs.exitIfStay, moveQuote = s.costs.exitIfMove;
  const exitDelta = ceilSigned(
    BigInt(moveQuote.numeratorMicrounits) * BigInt(stayQuote.denominator) -
      BigInt(stayQuote.numeratorMicrounits) * BigInt(moveQuote.denominator),
    BigInt(moveQuote.denominator) * BigInt(stayQuote.denominator));
  const incremental = upfront + exitDelta;
  // A possible exit saving cannot disguise fees above the user's cap.
  const feeLimitCharge = upfront + (exitDelta > 0n ? exitDelta : 0n);
  const net = interest - incremental;
  const calculation: Calculation = {
    valuationUnit: s.valuation.unit, valuationDecimals: 6,
    remainingSeconds: remaining.toString(), yearSeconds: YEAR_SECONDS.toString(),
    movableUnits: amount.toString(), protectedIdleReserveUnits: p.protectedIdleReserveUnits,
    aprDeltaBps: delta.toString(), interestNumerator: numerator.toString(), interestDenominator: denominator.toString(),
    additionalInterestMicrounits: interest.toString(), costBreakdown,
    upfrontCostMicrounits: upfront.toString(), exitCostDifferenceMicrounits: exitDelta.toString(),
    incrementalCostMicrounits: incremental.toString(), feeLimitChargeMicrounits: feeLimitCharge.toString(),
    netImprovementMicrounits: net.toString(),
    strategyAccounting: {...s.accounting, estimatedExitIfStayMicrounits: exitStay.toString(),
      estimatedExitIfMoveMicrounits: exitMove.toString()},
    estimate: true, simulated: evidence.some(e => e.simulated),
    assumptions: ['APR remains unchanged until the fixed approved deadline.',
      'Transfer downtime is zero; later execution must record actual time in transit.',
      'The full movable amount is deposited; all fees are funded separately from unreserved idle balances.',
      'A year is exactly 365 days. This estimate is not a promise of earnings.'],
  };
  if (remaining === 0n) return result('hold', 'EXPIRED_DEADLINE', 'The approved plan deadline has passed.', calculation);
  if (sameLocation(source, destination))
    return result('hold', 'ALREADY_AT_DESTINATION', 'The position is already in the proposed pool.', calculation);
  if (p.lastMoveCompletedAt !== null && now - BigInt(p.lastMoveCompletedAt) < BigInt(p.cooldownSeconds))
    return result('hold', 'COOLDOWN', 'Wait for the approved cooldown to finish.', calculation);
  if (feeLimitCharge > BigInt(p.maxFeeMicrounits))
    return blocked('FEE_LIMIT', 'Estimated incremental fees exceed the approved fee limit.', calculation);
  if (net <= 0n) return result('hold', 'NO_NET_IMPROVEMENT', 'Moving does not have a positive expected improvement after costs.', calculation);
  if (net < BigInt(p.minNetBenefitMicrounits))
    return result('hold', 'BELOW_MINIMUM_BENEFIT', 'Expected net improvement is below the approved minimum benefit.', calculation);
  return result('move', 'POSITIVE_NET_IMPROVEMENT', 'Expected net improvement is positive and meets the approved limits.', calculation);
}
