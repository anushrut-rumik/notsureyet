import assert from 'node:assert/strict';
import { test } from 'node:test';
import { decide } from '../.local/lending-build/decision.js';
import { exampleRun, FIXTURE_NOW, fixtureClock } from '../.local/lending-build/fixtures.js';
import { createRunInput, observations } from '../.local/lending-build/schema.js';
import { evaluateRun } from '../.local/lending-build/run.js';

const clone = (days = 60) => structuredClone(exampleRun(days));
const evaluate = run => evaluateRun(run, fixtureClock).decision;
const change = (mutate, days = 60) => {const run = clone(days); mutate(run); return evaluate(run);};
const assertCode = (decision, action, code) => {
  assert.equal(decision.action, action, JSON.stringify(decision));
  assert.equal(decision.code, code);
};

test('60-day fixture moves with exact conservative earnings and separately protected reserve', () => {
  const run = exampleRun(60), result = evaluateRun(run, fixtureClock);
  assertCode(result.decision, 'move', 'POSITIVE_NET_IMPROVEMENT');
  const c = result.decision.calculation;
  assert.equal(c.additionalInterestMicrounits, '3945205');
  assert.equal(c.incrementalCostMicrounits, '2000000');
  assert.equal(c.netImprovementMicrounits, '1945205');
  assert.equal(c.movableUnits, (800n * 10n ** 18n).toString());
  assert.equal(c.protectedIdleReserveUnits, (200n * 10n ** 18n).toString());
  assert.equal(c.remainingSeconds, (60n * 86400n).toString());
  assert.equal(c.yearSeconds, '31536000');
  assert.equal(c.simulated, true);
  assert.deepEqual(result.context, run.context);
  assert.equal(result.runId, run.runId);
  assert.equal(result.operationId, run.operationId);
  assert.deepEqual(result.observation, run.observation);
});

test('30-day fixture holds because extra interest does not cover costs', () => {
  const decision = evaluate(exampleRun(30));
  assertCode(decision, 'hold', 'NO_NET_IMPROVEMENT');
  assert.equal(decision.calculation.additionalInterestMicrounits, '1972602');
  assert.equal(decision.calculation.netImprovementMicrounits, '-27398');
});

test('rounding at the minimum benefit boundary is inclusive, never rounded up', () => {
  assertCode(change(r => {r.policy.minNetBenefitMicrounits = '1945205';}), 'move', 'POSITIVE_NET_IMPROVEMENT');
  assertCode(change(r => {r.policy.minNetBenefitMicrounits = '1945206';}), 'hold', 'BELOW_MINIMUM_BENEFIT');
});

test('an elapsed day reduces the fixed horizon; it never restarts a 60-day plan', () => {
  const run = clone();
  const tomorrow = (BigInt(FIXTURE_NOW) + 86400n).toString();
  for (const e of observations(run.snapshot)) e.observedAt = tomorrow;
  const decision = decide(run.policy, run.snapshot, () => tomorrow);
  assert.equal(decision.calculation.remainingSeconds, (59n * 86400n).toString());
  assert.equal(decision.calculation.additionalInterestMicrounits, '3879452');
});

for (const offset of [0n, -1n]) {
  test(`expired horizon ${offset} holds with zero remaining time`, () => {
    const decision = change(r => {r.policy.deadline = (BigInt(FIXTURE_NOW) + offset).toString(); r.policy.approvedAt = '1';});
    assertCode(decision, 'hold', 'EXPIRED_DEADLINE');
    assert.equal(decision.calculation.remainingSeconds, '0');
  });
}

test('freshness includes every price, rate, liquidity, reserve, exit quote and fee balance', () => {
  for (let index = 0; index < observations(clone().snapshot).length; index++) {
    assertCode(change(r => {observations(r.snapshot)[index].observedAt = (BigInt(FIXTURE_NOW) - 301n).toString();}),
      'blocked', 'STALE_OBSERVATION');
  }
});
test('freshness and observation-skew boundaries are inclusive', () => {
  assertCode(change(r => {r.policy.maxObservationSkewSeconds = '300';
    r.snapshot.valuation.evidence.observedAt = (BigInt(FIXTURE_NOW) - 300n).toString();}), 'move', 'POSITIVE_NET_IMPROVEMENT');
  assertCode(change(r => {r.snapshot.valuation.evidence.observedAt = (BigInt(FIXTURE_NOW) - 61n).toString();}),
    'blocked', 'OBSERVATION_SKEW');
});
test('future observations and snapshot assembly times are blocked', () => {
  const run = clone(); run.snapshot.valuation.evidence.observedAt = (BigInt(FIXTURE_NOW) + 1n).toString();
  assertCode(decide(run.policy, run.snapshot, fixtureClock), 'blocked', 'FUTURE_OBSERVATION');
  assertCode(change(r => {r.observation.assembledAt = (BigInt(FIXTURE_NOW) + 1n).toString();}), 'blocked', 'FUTURE_OBSERVATION_CONTEXT');
});

test('reserve must be idle: invested funds and existing commitments cannot substitute', () => {
  assertCode(change(r => {r.snapshot.reserve.idleBalanceUnits = '0';}), 'blocked', 'RESERVE_SHORTFALL');
  assertCode(change(r => {r.snapshot.reserve.committedUnits = '1';}), 'blocked', 'RESERVE_SHORTFALL');
  assertCode(change(r => {r.snapshot.reserve.chainId = '84532';}), 'blocked', 'RESERVE_LOCATION_MISMATCH');
});

function useManagedTokenForFees(run, extraIdleUnits = 0n) {
  const s = run.snapshot;
  s.reserve.idleBalanceUnits = (200n * 10n ** 18n + extraIdleUnits).toString();
  s.feeBalances = [{chainId: '11155111', assetId: 'CCIP-BnM', assetDecimals: 18,
    availableUnits: s.reserve.idleBalanceUnits, reservedUnits: run.policy.protectedIdleReserveUnits,
    evidence: {...s.reserve.evidence}}];
  for (const c of s.costs.components) {
    c.assetId = 'CCIP-BnM'; c.assetDecimals = 18;
    c.amountUnits = c.category === 'bridgeFee' ? (2n * 10n ** 18n).toString() : '0';
  }
}
test('token fees cannot consume the reserve but may use an independently idle surplus', () => {
  assertCode(change(r => useManagedTokenForFees(r)), 'blocked', 'INSUFFICIENT_FEE_BALANCE');
  assertCode(change(r => useManagedTokenForFees(r, 2n * 10n ** 18n)), 'move', 'POSITIVE_NET_IMPROVEMENT');
  assertCode(change(r => {useManagedTokenForFees(r); r.snapshot.feeBalances[0].reservedUnits = '0';}),
    'blocked', 'RESERVE_FEE_BALANCE_MISMATCH');
  assertCode(change(r => {useManagedTokenForFees(r); r.snapshot.feeBalances[0].availableUnits = (1000n * 10n ** 18n).toString();}),
    'blocked', 'RESERVE_FEE_BALANCE_MISMATCH');
});

test('unapproved source or destination chain/pool/token is blocked', () => {
  assertCode(change(r => {r.policy.approvedPools.pop();}), 'blocked', 'UNAPPROVED_LOCATION');
  assertCode(change(r => {r.policy.approvedPools.shift();}), 'blocked', 'UNAPPROVED_LOCATION');
  for (const field of ['chainId', 'poolId', 'tokenId']) {
    const decision = change(r => {r.snapshot.destinationPool.location[field] = field === 'chainId' ? '1' : 'other';});
    assert.equal(decision.action, 'blocked');
  }
});
test('source withdrawal liquidity and destination deposit capacity are different limits', () => {
  assertCode(change(r => {r.snapshot.currentPool.withdrawalAvailableUnits = (800n * 10n ** 18n - 1n).toString();}),
    'blocked', 'SOURCE_LIQUIDITY');
  assertCode(change(r => {r.snapshot.destinationPool.depositCapacityUnits = (800n * 10n ** 18n - 1n).toString();}),
    'blocked', 'DESTINATION_CAPACITY');
  // A destination token balance is intentionally not an input to a deposit check.
  assert.equal('withdrawalAvailableUnits' in clone().snapshot.destinationPool, false);
  assert.equal(evaluate(exampleRun(60)).action, 'move');
});
test('movable amount cannot exceed invested balance or user limit', () => {
  assertCode(change(r => {r.policy.maxMovableUnits = '1';}), 'blocked', 'MOVABLE_LIMIT');
  assertCode(change(r => {r.snapshot.position.investedUnits = '1';}), 'blocked', 'MOVABLE_LIMIT');
});

test('fee cap is inclusive and applies after upward rounding', () => {
  assertCode(change(r => {r.policy.maxFeeMicrounits = '2000000';}), 'move', 'POSITIVE_NET_IMPROVEMENT');
  assertCode(change(r => {r.policy.maxFeeMicrounits = '1999999';}), 'blocked', 'FEE_LIMIT');
  const decision = change(r => {r.snapshot.costs.components[0].assetDecimals = 18;
    r.snapshot.costs.components[0].assetId = 'DUST'; r.snapshot.costs.components[0].amountUnits = '1';
    r.snapshot.feeBalances.push({chainId: '11155111', assetId: 'DUST', assetDecimals: 18,
      availableUnits: '1', reservedUnits: '0', evidence: {...r.snapshot.reserve.evidence}});});
  assert.equal(decision.calculation.upfrontCostMicrounits, '2000001');
  assert.equal(decision.calculation.netImprovementMicrounits, '1945204');
});
test('cost categories are mandatory, unique and each is charged once', () => {
  assertCode(change(r => {r.snapshot.costs.components.pop();}), 'blocked', 'INVALID_RUN_INPUT');
  assertCode(change(r => {r.snapshot.costs.components[0].category = 'bridgeFee';}), 'blocked', 'COST_CATEGORIES');
  const decision = change(r => {for (const c of r.snapshot.costs.components) c.amountUnits = '100000';});
  assert.equal(decision.calculation.upfrontCostMicrounits, '700000');
});
test('fee funding is summed per chain and asset, using only unreserved available balances', () => {
  assertCode(change(r => {r.snapshot.feeBalances = [];}), 'blocked', 'MISSING_FEE_BALANCE');
  assertCode(change(r => {r.snapshot.feeBalances[0].availableUnits = '1999999';}), 'blocked', 'INSUFFICIENT_FEE_BALANCE');
  assertCode(change(r => {r.snapshot.feeBalances[0].reservedUnits = '8000001';}), 'blocked', 'INSUFFICIENT_FEE_BALANCE');
  assertCode(change(r => {r.snapshot.costs.components[0].amountUnits = '9000000';}), 'blocked', 'INSUFFICIENT_FEE_BALANCE');
  assertCode(change(r => {r.snapshot.feeBalances.push({...r.snapshot.feeBalances[0]});}), 'blocked', 'INVALID_FEE_BALANCE');
  assertCode(change(r => {r.snapshot.costs.components[0].priceMicrounitsPerToken = '1';}), 'blocked', 'FEE_VALUATION_MISMATCH');
});

test('eventual exit difference is incremental; common and sunk fees are not recharged', () => {
  const decision = change(r => {
    r.snapshot.costs.exitIfStay.numeratorMicrounits = '5000000';
    r.snapshot.costs.exitIfMove.numeratorMicrounits = '5500000';
    r.snapshot.accounting.initialDepositCostMicrounits = '10000000';
    r.snapshot.accounting.realizedWithdrawalCostMicrounits = '3000000';
    r.snapshot.accounting.priorRebalanceCostMicrounits = '4000000';
  });
  assert.equal(decision.calculation.incrementalCostMicrounits, '2500000');
  assert.equal(decision.calculation.netImprovementMicrounits, '1445205');
  assert.equal(decision.calculation.strategyAccounting.initialDepositCostMicrounits, '10000000');
  assert.equal(decision.calculation.strategyAccounting.realizedWithdrawalCostMicrounits, '3000000');
});
test('exit comparison rounds the exact incremental cost up, preserving common-cost cancellation', () => {
  const decision = change(r => {
    r.snapshot.costs.exitIfStay = {...r.snapshot.costs.exitIfStay, numeratorMicrounits: '2', denominator: '3'};
    r.snapshot.costs.exitIfMove = {...r.snapshot.costs.exitIfMove, numeratorMicrounits: '4', denominator: '3'};
  });
  assert.equal(decision.calculation.exitCostDifferenceMicrounits, '1');
  const equal = change(r => {
    r.snapshot.costs.exitIfStay = {...r.snapshot.costs.exitIfStay, numeratorMicrounits: '2', denominator: '3'};
    r.snapshot.costs.exitIfMove = {...r.snapshot.costs.exitIfMove, numeratorMicrounits: '2', denominator: '3'};
  });
  assert.equal(equal.calculation.exitCostDifferenceMicrounits, '0');
});
test('exit savings do not hide an excessive upfront fee from the fee cap', () => {
  assertCode(change(r => {r.policy.maxFeeMicrounits = '1999999';
    r.snapshot.costs.exitIfStay.numeratorMicrounits = '2000000';}), 'blocked', 'FEE_LIMIT');
});
test('zero net is a hold even with zero minimum; negative APR differences round conservatively', () => {
  assertCode(change(r => {r.snapshot.destinationPool.aprBps = '400';
    r.snapshot.costs.components.forEach(c => {c.amountUnits = '0';}); r.policy.minNetBenefitMicrounits = '0';}),
    'hold', 'NO_NET_IMPROVEMENT');
  const decision = change(r => {r.snapshot.destinationPool.aprBps = '100';});
  assert.equal(decision.calculation.additionalInterestMicrounits, '-3945206');
  assertCode(decision, 'hold', 'NO_NET_IMPROVEMENT');
});

test('cooldown completion is inclusive and future completion timestamps are rejected', () => {
  assertCode(change(r => {r.policy.lastMoveCompletedAt = (BigInt(FIXTURE_NOW) - 86399n).toString();}), 'hold', 'COOLDOWN');
  assertCode(change(r => {r.policy.lastMoveCompletedAt = (BigInt(FIXTURE_NOW) - 86400n).toString();}), 'move', 'POSITIVE_NET_IMPROVEMENT');
  assertCode(change(r => {r.policy.lastMoveCompletedAt = (BigInt(FIXTURE_NOW) + 1n).toString();}), 'blocked', 'INVALID_LAST_MOVE');
});
for (const status of ['prepared', 'withdrawing', 'in_transit', 'depositing', 'unknown', 'failed']) {
  test(`pending ${status} operation blocks overlapping moves, including its own ID`, () => {
    assertCode(change(r => {r.snapshot.pendingOperation = {operationId: r.operationId, status};}), 'blocked', 'PENDING_OPERATION');
  });
}

test('missing or malformed required financial data fails closed, without exceptions', () => {
  for (const value of [undefined, null, {}, {valuation: {}}, 'text']) {
    assertCode(decide(clone().policy, value, fixtureClock), 'blocked', 'INVALID_INPUT');
  }
  for (const bad of ['-1', '01', '1.1', 'NaN', 'not-an-integer', '9'.repeat(79), 400, null]) {
    const run = clone(); run.snapshot.currentPool.aprBps = bad;
    assertCode(decide(run.policy, run.snapshot, fixtureClock), 'blocked', 'INVALID_INPUT');
  }
  for (const remove of [r => {delete r.snapshot.pendingOperation;}, r => {delete r.snapshot.currentPool.withdrawalAvailableUnits;},
    r => {delete r.snapshot.destinationPool.depositCapacityUnits;}, r => {delete r.snapshot.costs.exitIfMove;}]) {
    const decision = change(remove);
    assertCode(decision, 'blocked', 'INVALID_RUN_INPUT');
    assert.ok(decision.issues.some(issue => issue.startsWith('snapshot.')));
  }
});
test('invalid clocks are blocked; the injected clock is read exactly once', () => {
  const r = clone();
  for (const value of ['1.1', '-1', 'no', undefined]) {
    assertCode(decide(r.policy, r.snapshot, () => value), 'blocked', 'INVALID_CLOCK');
  }
  assertCode(decide(r.policy, r.snapshot, () => {throw new Error('clock');}), 'blocked', 'INVALID_CLOCK');
  let reads = 0;
  evaluateRun(r, () => {reads++; return FIXTURE_NOW;}); assert.equal(reads, 1);
});
test('financial integers above JS safe-integer range preserve exact precision', () => {
  const r = clone(); r.policy.maxMovableUnits = r.snapshot.position.movableUnits = r.snapshot.position.investedUnits = '9007199254740993';
  r.policy.protectedIdleReserveUnits = '0'; r.snapshot.valuation.tokenDecimals = 0;
  r.snapshot.currentPool.withdrawalAvailableUnits = r.snapshot.destinationPool.depositCapacityUnits = '9007199254740993';
  r.snapshot.costs.components.forEach(c => {c.amountUnits = '0';});
  const c = evaluate(r).calculation;
  assert.equal(c.additionalInterestMicrounits, (9007199254740993n * 1000000n * 300n * 60n / (10000n * 365n)).toString());
});

test('run schema isolates immutable inputs and bound immutable outputs', () => {
  const original = clone(), frozen = createRunInput(original);
  original.policy.deadline = '1';
  assert.notEqual(frozen.policy.deadline, original.policy.deadline);
  assert.throws(() => {frozen.policy.deadline = '1';}, TypeError);
  assert.throws(() => {frozen.snapshot.costs.components[0].amountUnits = '1';}, TypeError);
  const result = evaluateRun(frozen, fixtureClock);
  assert.throws(() => {result.context.userId = 'other';}, TypeError);
  assert.throws(() => {result.decision.calculation.netImprovementMicrounits = '1';}, TypeError);
  assert.deepEqual(evaluateRun(frozen, fixtureClock), result);
});
test('different users/runs remain isolated without rewriting shared configuration', () => {
  const a = clone(), b = clone();
  b.runId = 'second-user-run'; b.context.userId = b.policy.userId = b.snapshot.position.userId = 'second-user';
  b.context.walletAddress = b.policy.walletAddress = b.snapshot.position.walletAddress = '0x2222222222222222222222222222222222222222';
  const ra = evaluateRun(a, fixtureClock), rb = evaluateRun(b, fixtureClock);
  assert.equal(ra.decision.action, 'move'); assert.equal(rb.decision.action, 'move');
  assert.notEqual(ra.runId, rb.runId); assert.notEqual(ra.context.userId, rb.context.userId);
  assert.equal(ra.networkConfigVersion, rb.networkConfigVersion);
});
test('identity, policy version, observation, operation and configuration mismatches are blocked', () => {
  for (const mutate of [r => {r.context.userId = 'other';}, r => {r.context.walletAddress = '0x2222222222222222222222222222222222222222';},
    r => {r.context.policyVersion = 'other';}]) {
    assertCode(change(mutate), 'blocked', 'CONTEXT_MISMATCH');
  }
  assertCode(change(r => {r.snapshot.position.userId = 'other';}), 'blocked', 'OWNER_MISMATCH');
  assertCode(change(r => {r.observation.snapshotId = 'other';}), 'blocked', 'OBSERVATION_CONTEXT_MISMATCH');
  assertCode(change(r => {r.networkConfigVersion = 'other';}), 'blocked', 'NETWORK_CONFIG_VERSION');
  assertCode(change(r => {r.operationId = null;}), 'blocked', 'MISSING_OPERATION_ID');
  assertCode(change(r => {r.snapshot.pendingOperation = {operationId: 'other', status: 'in_transit'};}), 'blocked', 'OPERATION_MISMATCH');
});
test('simulation, unit and model limitations are explicit', () => {
  assertCode(change(r => {r.policy.allowSimulatedInputs = false;}), 'blocked', 'SIMULATED_INPUT_DISALLOWED');
  assertCode(change(r => {r.snapshot.valuation.unit = 'USD';}), 'blocked', 'VALUATION_UNIT_MISMATCH');
  assertCode(change(r => {r.snapshot.model.transferDowntimeSeconds = '60';}), 'blocked', 'INVALID_RUN_INPUT');
});
