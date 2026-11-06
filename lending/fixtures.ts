import { networkConfig } from './networks.js';
import { costCategories, createRunInput, type RunInput } from './schema.js';

export const FIXTURE_NOW = '1793966400'; // 2026-11-06 12:00:00 UTC; deliberately frozen.
export const fixtureClock = () => FIXTURE_NOW;

/** Arithmetic only. These addresses, balances and prices are NOT live observations. */
export function exampleRun(days: 30 | 60): RunInput {
  const [source, destination] = networkConfig.networks;
  const location = ({chainId, poolId, tokenId}: typeof source) => ({chainId, poolId, tokenId});
  const evidence = (label: string) => ({
    observedAt: FIXTURE_NOW, source: `arithmetic-fixture:${label}; fictional, not a live quote`, simulated: true,
  });
  const walletAddress = '0x1111111111111111111111111111111111111111';
  return createRunInput({
    interfaceVersion: '1', runId: `fixture-${days}-days`, networkConfigVersion: networkConfig.version,
    context: {userId: 'fixture-user', walletAddress, walletBindingId: 'fixture-wallet-binding', policyVersion: 'fixture-policy-v1'},
    observation: {snapshotId: `fixture-snapshot-${days}`, assembledAt: FIXTURE_NOW, reference: 'arithmetic-fixture-v1'},
    operationId: `fixture-operation-${days}`,
    policy: {
      version: 'fixture-policy-v1', approvalId: 'fixture-approval', userId: 'fixture-user', walletAddress,
      approvedAt: (BigInt(FIXTURE_NOW) - 1n).toString(),
      deadline: (BigInt(FIXTURE_NOW) + BigInt(days) * 86_400n).toString(),
      valuationUnit: 'SIM_USD', allowSimulatedInputs: true,
      approvedPools: [location(source), location(destination)],
      protectedIdleReserveUnits: (200n * 10n ** 18n).toString(), maxMovableUnits: (800n * 10n ** 18n).toString(),
      minNetBenefitMicrounits: '1000000', maxFeeMicrounits: '3000000',
      maxObservationAgeSeconds: '300', maxObservationSkewSeconds: '60',
      cooldownSeconds: '86400', lastMoveCompletedAt: null,
    },
    snapshot: {
      id: `fixture-snapshot-${days}`,
      valuation: {unit: 'SIM_USD', decimals: 6, tokenDecimals: 18, priceMicrounitsPerToken: '1000000',
        evidence: evidence('one-CCIP-BnM-equals-one-simulated-dollar')},
      position: {id: 'fixture-single-position', userId: 'fixture-user', walletAddress, location: location(source),
        investedUnits: (800n * 10n ** 18n).toString(), movableUnits: (800n * 10n ** 18n).toString(),
        evidence: evidence('800-invested-tokens')},
      reserve: {chainId: source.chainId, tokenId: source.tokenId,
        idleBalanceUnits: (200n * 10n ** 18n).toString(), committedUnits: '0', evidence: evidence('200-idle-tokens')},
      currentPool: {location: location(source), aprBps: '400', withdrawalAvailableUnits: (800n * 10n ** 18n).toString(),
        evidence: evidence('current-4-percent-apr-and-withdrawal-liquidity')},
      destinationPool: {location: location(destination), aprBps: '700', depositCapacityUnits: (800n * 10n ** 18n).toString(),
        evidence: evidence('destination-7-percent-apr-and-deposit-capacity')},
      costs: {
        components: costCategories.map(category => ({category,
          chainId: ['destinationApprovalGas', 'destinationGas', 'depositFee'].includes(category) ? destination.chainId : source.chainId,
          assetId: 'SIM_FEE_ASSET',
          assetDecimals: 6, amountUnits: category === 'bridgeFee' ? '2000000' : '0', priceMicrounitsPerToken: '1000000',
          evidence: evidence(category === 'bridgeFee' ? '2-simulated-dollars-bridge-fee' : `${category}-explicit-zero`)})),
        exitIfStay: {numeratorMicrounits: '0', denominator: '1', evidence: evidence('exit-if-stay-zero')},
        exitIfMove: {numeratorMicrounits: '0', denominator: '1', evidence: evidence('exit-if-move-zero')},
      },
      feeBalances: [source, destination].map(network => ({chainId: network.chainId, assetId: 'SIM_FEE_ASSET', assetDecimals: 6,
        availableUnits: '10000000', reservedUnits: '0', evidence: evidence('separate-simulated-fee-funding')})),
      accounting: {initialDepositCostMicrounits: '0', realizedWithdrawalCostMicrounits: '0', priorRebalanceCostMicrounits: '0'},
      pendingOperation: null, model: {rates: 'unchanged', transferDowntimeSeconds: '0'},
    },
  });
}
