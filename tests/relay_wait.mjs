// Execute the actual relay with virtual I/O/time; no model or Docker calls.
import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const code=fs.readFileSync(new URL('../evo_arena/fixtures/harness/relay.cjs',import.meta.url),'utf8');
async function scenario(disconnect=false){
  let handle,reads=0,waited=0,ended=false,status;
  const res={destroyed:false,headersSent:false,writeHead(s){status=s;},end(body){ended=true;this.body=body;}};
  const request={method:'POST',url:'/v1/responses',async *[Symbol.asyncIterator](){yield Buffer.from('{}');}};
  const modules={
    'node:http':{createServer(fn){handle=fn;return {listen(){}};}},
    'node:crypto':{randomUUID:()=> 'fixture'},
    'node:fs/promises':{
      writeFile:async()=>{},rename:async()=>{},unlink:async()=>{},
      readFile:async()=>{
        if(++reads<=1000)throw Object.assign(new Error('pending'),{code:'ENOENT'});
        return JSON.stringify({status:200,type:'application/json',body:'completed'});
      },
    },
    'node:timers/promises':{setTimeout:async ms=>{waited+=ms;if(disconnect&&waited>=500)res.destroyed=true;}},
  };
  vm.runInNewContext(code,{Buffer,require:name=>modules[name]});
  handle(request,res);
  // Let the finite virtual timer/microtask chain finish.
  await new Promise(resolve=>setImmediate(resolve));
  if(disconnect){assert.equal(ended,false);assert.equal(waited,500);}
  else{assert.equal(status,200);assert.equal(res.body,'completed');assert.ok(waited>90000);}
}
await scenario();await scenario(true);
console.log('PASS: relay waits beyond 90s and exits on caller disconnect (virtual time).');
