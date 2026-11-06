// Invoke the project's official binary directly and preserve its exit code (including failed tests).
import {createRequire} from 'node:module';
import {existsSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {spawnSync} from 'node:child_process';
const require=createRequire(import.meta.url);
const [tool,...args]=process.argv.slice(2);
if(!['forge','cast'].includes(tool))throw Error('Choose forge or cast');
const arch=process.arch==='x64'?'amd64':process.arch;
const binary=tool+(process.platform==='win32'?'.exe':'');
let executable;
try{executable=require.resolve(`@foundry-rs/${tool}-${process.platform}-${arch}/bin/${binary}`);}catch{}
if(!executable){
 const directory=dirname(require.resolve(`@foundry-rs/${tool}/package.json`));
 executable=[join(directory,binary),join(directory,'..','dist',binary)].find(p=>existsSync(p));
}
if(!executable)throw Error('Foundry binary missing. Run npm ci with optional dependencies and install scripts enabled.');
const result=spawnSync(executable,args,{stdio:'inherit'});
if(result.error)console.error(result.error.message);
process.exit(result.status??1);
