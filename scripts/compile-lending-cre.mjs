import {spawnSync} from 'node:child_process';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const result=spawnSync(resolve(root,'node_modules/.bin/bun'),[
 'node_modules/@chainlink/cre-sdk/bin/cre-compile.ts','cre-lending/workflow/main.ts','.local/lending-cre/main.wasm'],
 {cwd:root,stdio:'inherit',env:{...process.env,PATH:resolve(root,'node_modules/.bin')+':'+process.env.PATH}});
process.exit(result.status??1);
