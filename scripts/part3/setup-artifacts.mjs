// Reuses Part 2's validated public plan format. Creates NEW Part 3 review files only.
// Never overwrites or migrates an ongoing Part 2 deployment and never signs/sends.
import {spawnSync} from 'node:child_process';
import {readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createPublicClient,http,parseAbi} from 'viem';
import {testnets} from '../../.local/lending-build/testnets.js';
import {verifyRuntimeCode} from './code.mjs';
const [planPath,directory,existingManifestPath]=process.argv.slice(2);
if(!planPath||!directory||!existingManifestPath)throw Error('Usage: setup-artifacts.mjs completed-public-plan.json new-part3-review-directory confirmed-existing-manifest.json');
const existing=JSON.parse(await readFile(existingManifestPath,'utf8')),plan=JSON.parse(await readFile(planPath,'utf8'));
if(existing.scope!=='dedicated-test-only'||existing.owner?.toLowerCase()!==plan.owner?.toLowerCase()||existing.chains?.length!==2||new Set(existing.chains.map(c=>c.chainId)).size!==2)throw Error('Existing dedicated owner/pools must match the reviewed plan');
const route=spawnSync(process.execPath,['scripts/part3/cli.mjs','route'],{encoding:'utf8'});
if(route.status!==0)throw Error('Current lane/token verification failed; no setup artifact created');
const generated=spawnSync(process.execPath,['scripts/lending-plan.mjs',resolve(planPath),resolve(directory)],{stdio:'inherit'});
if(generated.status!==0)process.exit(generated.status??1);
const path=resolve(directory,'deployment-review.json'),review=JSON.parse(await readFile(path,'utf8'));
for(const deployment of review.deployments){
 const previous=existing.chains.find(c=>c.chainId===deployment.network.chainId);
 if(!previous)throw Error('Missing confirmed owned test pool');
 const client=createPublicClient({transport:http(deployment.network.rpc,{timeout:15000,retryCount:1})});
 if(String(await client.getChainId())!==deployment.network.chainId)throw Error('Wrong RPC chain');
 const block=await client.getBlock({blockTag:'finalized'}),artifact=JSON.parse(await readFile('contracts/out/MockPool.sol/MockPool.json','utf8'));
 verifyRuntimeCode(await client.getCode({address:previous.pool,blockNumber:block.number}),artifact);
 const abi=parseAbi(['function owner() view returns(address)','function asset() view returns(address)']);
 const [owner,token]=await Promise.all(['owner','asset'].map(functionName=>client.readContract({address:previous.pool,abi,functionName,blockNumber:block.number})));
 if(owner.toLowerCase()!==review.signer.toLowerCase()||token.toLowerCase()!==deployment.network.token.toLowerCase())throw Error('Existing pool owner/asset mismatch');
 deployment.script='contracts/script/lending/part3/DeployExecutionLending.s.sol:DeployExecutionLending';
 deployment.expectedEffect='Deploy only a new execution wallet for the confirmed owned pool. Do not modify or migrate the old wallet.';
 deployment.signature='runWallet((address,bytes32,bytes32,uint256,uint256,uint256,uint256),address)';
 deployment.arguments=[deployment.argument,previous.pool];delete deployment.argument;
 deployment.method=deployment.signature;deployment.existingPool=previous.pool;
 deployment.signing='User-run only. Use the dedicated faucet-funded owner and its existing wallet-controlled signing environment.';
}
review.scope='dedicated-test-only';review.part=3;review.routeEvidence=JSON.parse(route.stdout);
review.forwarderAuthentication='Verified testnet mock forwarder + independent owner EIP-712 instruction signature. Mock metadata is not DON authentication.';
review.review='Review this carefully and execute it only from your own wallet-controlled environment.';
review.signingBoundary='This skill never signs or sends. You must sign and broadcast from your own wallet.';
await writeFile(path,JSON.stringify(review,null,2)+'\n');
const manifestPath=resolve(directory,'manifest.json'),manifest=JSON.parse(await readFile(manifestPath,'utf8'));
for(const chain of manifest.chains){const prior=existing.chains.find(c=>c.chainId===chain.chainId);chain.pool=prior.pool;chain.poolDeploymentTx=prior.poolDeploymentTx;}
await writeFile(manifestPath,JSON.stringify(manifest,null,2)+'\n');
console.log('Part 3 setup review: '+path+'. No transaction was sent.');
