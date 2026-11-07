// Server-owned setup policy: safe reads are available only for this two-testnet demo.
export function setupReadTag(manifest) {
 if(manifest.setupConfirmation!=='safe')return 'finalized';
 if(manifest.scope!=='dedicated-test-only'||manifest.chains?.length!==2||
    new Set(manifest.chains.map(c=>c.chainId)).size!==2||
    !manifest.chains.every(c=>['11155111','84532'].includes(c.chainId)))throw Error('Safe setup reads require both approved testnets');
 return 'safe';
}

// Application policy for the dedicated faucet-funded two-testnet demo only.
export function testnetReadTag(manifest) {
 if(manifest.scope==='dedicated-test-only'&&manifest.chains?.length===2&&
    new Set(manifest.chains.map(c=>c.chainId)).size===2&&
    manifest.chains.every(c=>['11155111','84532'].includes(c.chainId)))return 'latest';
 return setupReadTag(manifest);
}
