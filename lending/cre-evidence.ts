/** Keep each evidence log below CRE's 1 KiB line limit. ASCII escapes preserve
 * arbitrary user IDs and prevent multibyte characters from exceeding the limit. */
export function resultLogLines(json: string): readonly string[] {
  const ascii=json.replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
  const count=Math.ceil(ascii.length/800);
  return Array.from({length:count},(_,i)=>`PART2_RESULT_CHUNK=${i}/${count}:${ascii.slice(i*800,(i+1)*800)}`);
}
/** Accept complete consistent chunks only. Missing/truncated evidence fails closed. */
export function readLoggedResult(output: string): unknown {
  const chunks=new Map<number,string>(); let expected=0;
  const plain=output.replace(/\u001b\[[0-9;]*m/g,'');
  for(const line of plain.split('\n')){
    const match=/PART2_RESULT_CHUNK=([0-9]+)\/([0-9]+):(.*)$/.exec(line.replace(/\r$/,''));
    if(!match)continue;
    const index=Number(match[1]),count=Number(match[2]),value=match[3]!;
    if(count<1 || count>1000 || index>=count || (expected!==0 && expected!==count) ||
      (chunks.has(index) && chunks.get(index)!==value))throw Error('Inconsistent CRE evidence logs');
    expected=count; chunks.set(index,value);
  }
  if(expected===0 || chunks.size!==expected)throw Error('Missing complete CRE result logs');
  return JSON.parse(Array.from({length:expected},(_,i)=>chunks.get(i)).join(''));
}
