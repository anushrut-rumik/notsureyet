import { encodeAbiParameters, parseAbiParameters, type Hex } from 'viem';
// The consumer decodes (Instruction, bytes), NOT a function selector plus arguments.
export const instructionParameters = parseAbiParameters('(bytes32 operationId,uint256 sourceChainId,address consumer,uint64 destinationSelector,address destination,address token,uint256 amount,bytes32 policyVersion,uint256 expiry) instruction,bytes signature');
export type Instruction = {operationId: Hex; sourceChainId: bigint; consumer: Hex; destinationSelector: bigint;
  destination: Hex; token: Hex; amount: bigint; policyVersion: Hex; expiry: bigint};
export function encodeInstructionReport(instruction: Instruction, signature: Hex): Hex {
  return encodeAbiParameters(instructionParameters, [instruction, signature]);
}
