/** Official Chainlink Directory / CRE forwarder directory, checked 2026-11-06.
 * These are infrastructure addresses, NEVER our pool/wallet deployments. */
export const testnets = [
  {chainId: '11155111', name: 'ethereum-testnet-sepolia', selector: '16015286601757825753',
    rpc: 'https://ethereum-sepolia-rpc.publicnode.com',
    router: '0x0BF3dE8c5D3e8A2B34D2BEeB17ABfCeBaf363A59',
    token: '0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05',
    link: '0x779877A7B0D9E8603169DdbD7836e478b4624789',
    forwarder: '0x15fC6ae953E024d975e77382eEeC56A9101f9F88'},
  {chainId: '84532', name: 'ethereum-testnet-sepolia-base-1', selector: '10344971235874465080',
    rpc: 'https://base-sepolia-rpc.publicnode.com',
    router: '0xD3b06cEbF099CE7DA4AcCf578aaebFDBd6e88a93',
    token: '0x88A2d74F47a237a62e7A51cdDa67270CE381555e',
    link: '0xE4aB69C077896252FAFBD49EFD26B5D171A32410',
    forwarder: '0x82300bd7c3958625581cc2f77bc6464dcecdf3e5'},
] as const;
