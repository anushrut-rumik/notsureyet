// Arithmetic-only adapter fixture. Addresses are NOT deployed contracts.
import {encodeAbiParameters,parseAbiParameters,keccak256,toBytes} from 'viem';
import {exampleRun,FIXTURE_NOW} from '../../.local/lending-build/fixtures.js';
import {testnets} from '../../.local/lending-build/testnets.js';
export function payloadFixture(days=60){
 const run=JSON.parse(JSON.stringify(exampleRun(days)));
 const workflowId=keccak256(toBytes('arithmetic-workflow'));
 const chains=testnets.map((n,i)=>({chainId:n.chainId,wallet:'0x'+String(i+2).repeat(40),pool:'0x'+String(i+4).repeat(40),
  blockNumber:'10',blockHash:'0x'+'ab'.repeat(32),binding:'0x'+'00'.repeat(32),
  values:[n.chainId,'10',FIXTURE_NOW,i?'70000000000000000000000000':'40000000000000000000000000',
   i?'0':String(800n*10n**18n),String(200n*10n**18n),i?'0':String(800n*10n**18n),String(800n*10n**18n),
   String(10n*10n**18n),String(10n*10n**18n),'0',String(BigInt(run.context.walletAddress))],
  tokenDecimals:18,linkDecimals:18,quoteLinkUnits:String(2n*10n**18n)}));
 chains.forEach((c,i)=>{const n=testnets[i];c.binding=keccak256(encodeAbiParameters(
  parseAbiParameters('address,address,address,address,address,address,bytes32,bytes32,uint256,uint256,address'),
  [run.context.walletAddress,c.pool,n.token,n.link,n.router,n.forwarder,workflowId,keccak256(toBytes(run.policy.version)),
   BigInt(run.policy.protectedIdleReserveUnits),BigInt(run.policy.maxMovableUnits),chains[1-i].wallet]));});
 run.snapshot.costs.components.forEach(c=>{c.assetId='LINK';c.assetDecimals=18;c.amountUnits=c.category==='bridgeFee'?String(2n*10n**18n):'0';});
 run.snapshot.feeBalances=chains.flatMap(c=>['LINK','ETH_GAS'].map(assetId=>({chainId:c.chainId,assetId,assetDecimals:18,
  availableUnits:c.values[8],reservedUnits:'0',evidence:{observedAt:FIXTURE_NOW,source:'arithmetic-fixture:fee-balance',simulated:true}})));
 return {scenario:'ARITHMETIC ADAPTER FIXTURE; no deployed contracts',workflowId,run,chains};
}
