// Compare runtime instructions while masking only compiler-declared immutable values.
// The separate binding() check verifies the actual immutable owner/pool/router/policy values.
export function verifyRuntimeCode(actual,artifact){
 const deployed=artifact.deployedBytecode;
 if(typeof actual!=='string'||!deployed?.object)throw Error('Missing finalized runtime code or compiler artifact');
 const expected=deployed.object.replace(/^0x/,''),received=actual.replace(/^0x/,'');
 if(expected.length!==received.length)throw Error('Unexpected execution contract runtime length');
 const normalize=code=>{const bytes=code.toLowerCase().split('');
  for(const references of Object.values(deployed.immutableReferences??{}))for(const {start,length} of references){
   if(!Number.isSafeInteger(start)||!Number.isSafeInteger(length)||start<0||length<1||(start+length)*2>bytes.length)throw Error('Invalid immutable reference');
   bytes.fill('0',start*2,(start+length)*2);
  }
  return bytes.join('');
 };
 if(normalize(expected)!==normalize(received))throw Error('Deployed code is not the reviewed Part 3 contract; Part 2 wallets cannot execute rebalances');
 return true;
}
