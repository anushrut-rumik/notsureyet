import { z } from 'zod';
import { decide, type Decision } from './decision.js';
import { networkConfig } from './networks.js';
import { createRunInput, deepFreeze, observations, type Clock, type RunInput } from './schema.js';

export interface RunResult {
  readonly interfaceVersion: '1';
  readonly runId: string | null;
  readonly networkConfigVersion: string | null;
  readonly context: RunInput['context'] | null;
  readonly observation: RunInput['observation'] | null;
  readonly operationId: string | null;
  readonly decision: Decision;
}

/** Application/CRE boundary. Validation is structural, NOT authentication.
 * The caller must supply authenticated identity and a stored user-approved policy.
 * A move is advice, never an authorization or an atomic operation claim.
 */
export function evaluateRun(input: unknown, clock: Clock): RunResult {
  let run: RunInput;
  try {
    run = createRunInput(input);
  } catch (error) {
    return deepFreeze({interfaceVersion: '1', runId: null, networkConfigVersion: null,
      context: null, observation: null, operationId: null, decision: {
        action: 'blocked', code: 'INVALID_RUN_INPUT', reason: 'Run input is missing required fields or has invalid data.',
        evaluatedAt: null, calculation: null,
        issues: error instanceof z.ZodError ? error.issues.map(issue => `${issue.path.join('.')}: ${issue.message}`) : [],
      }});
  }
  const bind = (decision: Decision): RunResult => deepFreeze({
    interfaceVersion: '1', runId: run.runId, networkConfigVersion: run.networkConfigVersion,
    context: run.context, observation: run.observation, operationId: run.operationId, decision,
  });
  const blocked = (code: string, reason: string) => bind({
    action: 'blocked', code, reason, evaluatedAt: null, calculation: null, issues: [],
  });
  if (run.networkConfigVersion !== networkConfig.version)
    return blocked('NETWORK_CONFIG_VERSION', 'Run and static network configuration versions differ.');
  if (run.context.userId !== run.policy.userId ||
    run.context.walletAddress.toLowerCase() !== run.policy.walletAddress.toLowerCase() ||
    run.context.policyVersion !== run.policy.version)
    return blocked('CONTEXT_MISMATCH', 'Authenticated context must match the approved policy identity and version.');
  if (run.observation.snapshotId !== run.snapshot.id ||
    observations(run.snapshot).some(e => BigInt(e.observedAt) > BigInt(run.observation.assembledAt)))
    return blocked('OBSERVATION_CONTEXT_MISMATCH', 'Snapshot identity or assembly time does not match its observations.');
  if (run.snapshot.pendingOperation !== null && run.operationId !== run.snapshot.pendingOperation.operationId)
    return blocked('OPERATION_MISMATCH', 'The run must reference the pending operation being observed.');
  const decision = decide(run.policy, run.snapshot, clock);
  if (decision.evaluatedAt !== null && BigInt(run.observation.assembledAt) > BigInt(decision.evaluatedAt))
    return blocked('FUTURE_OBSERVATION_CONTEXT', 'Snapshot assembly time cannot be in the future.');
  if (decision.action === 'move' && run.operationId === null)
    return blocked('MISSING_OPERATION_ID', 'A move candidate requires an application-assigned operation ID.');
  return bind(decision);
}
