// Same injected MetaMask EIP-712 mechanism as the existing setup UI. No transaction submission.
import {createServer} from 'node:http';
import {randomBytes} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {buildInstruction,canonicalJson,signingRequest} from '../../.local/lending-build/part3/report.js';
const directory=resolve(process.argv[2]??''),root=resolve(fileURLToPath(new URL('../../integrations/lending-part3/',import.meta.url)));
const unsigned=JSON.parse(await readFile(resolve(directory,'unsigned.json'),'utf8'));
if(canonicalJson(buildInstruction(unsigned.observation,unsigned.instruction.expiry))!==canonicalJson(unsigned.instruction))throw Error('Unsigned artifact binding mismatch');
const typed=signingRequest(unsigned),token=randomBytes(24).toString('hex');
const server=createServer(async(req,res)=>{
 try{
  const base='/'+token+'/',url=new URL(req.url,'http://127.0.0.1');
  if(req.method!=='GET'||req.headers.host!==`127.0.0.1:${server.address().port}`||!url.pathname.startsWith(base)){res.writeHead(403);res.end();return;}
  const path=url.pathname.slice(base.length);let body,type;
  if(path==='payload'){body=JSON.stringify({unsigned,typed},(_,v)=>typeof v==='bigint'?String(v):v);type='application/json';}
  else if(path===''||path==='sign.js'){body=await readFile(resolve(root,path===''?'index.html':'sign.js'));type=path===''?'text/html':'text/javascript';}
  else{res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
   'Content-Security-Policy':"default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"});res.end(body);
 }catch{res.writeHead(500);res.end('Unable to load public signing artifact');}
});
server.listen(0,'127.0.0.1',()=>console.log(`Review and sign the exact instruction in MetaMask: http://127.0.0.1:${server.address().port}/${token}/`));
