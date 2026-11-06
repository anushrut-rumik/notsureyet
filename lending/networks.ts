import { deepFreeze } from './schema.js';

// Logical deployment slots, NOT deployed addresses or proof of a live CCIP lane.
// Resolve addresses/selectors and verify token/lane support during later parts.
export const networkConfig = deepFreeze({
  version: 'testnet-slots-v1',
  networks: [
    {chainId: '11155111', name: 'Ethereum Sepolia', poolId: 'mock-lending-sepolia', tokenId: 'CCIP-BnM'},
    {chainId: '84532', name: 'Base Sepolia', poolId: 'mock-lending-base-sepolia', tokenId: 'CCIP-BnM'},
  ],
});
