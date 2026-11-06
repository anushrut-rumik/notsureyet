// Solidity source packages only: avoid running transitive JavaScript build tooling in contract packages.
import {readFile, mkdir, writeFile, rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
const root=fileURLToPath(new URL('../',import.meta.url));
const packages=JSON.parse(await readFile(new URL('../contracts/dependencies.lock.json',import.meta.url),'utf8'));
for(const pkg of packages){
 const directory=resolve(root,'contracts/lib',pkg.directory);
 const response=await fetch(pkg.tarball);
 if(!response.ok)throw Error(`Could not download ${pkg.name}@${pkg.version}`);
 const bytes=Buffer.from(await response.arrayBuffer());
 const integrity='sha512-'+createHash('sha512').update(bytes).digest('base64');
 if(integrity!==pkg.integrity)throw Error(`Integrity mismatch for ${pkg.name}`);
 await mkdir(directory,{recursive:true});
 const archive=resolve(root,'contracts/lib',pkg.directory+'.tgz');
 await writeFile(archive,bytes);
 execFileSync('tar',['-xzf',archive,'--strip-components=1','-C',directory]);
 await rm(archive);
 console.log(`${pkg.name}@${pkg.version}: verified and extracted`);
}
