const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const elements=new Map();
for(const id of ['memory-target','memory-backend','memory-refresh','memory-release','memory-stop','memory-recover','memory-status']) elements.set(id,{value:id==='memory-target'?'comfyui':'sdxl',textContent:'',disabled:false,events:{},addEventListener(name,handler){this.events[name]=handler;}});
const requests=[];let blocked=false;
let state={ram_available_mib:52000,ram_used_mib:12000,swap_used_mib:8192,swap_free_mib:0,vram_used_mib:9000,vram_free_mib:3288,loaded_model:'strata',state:'READY',required_ram_mib:20480,ready_to_start:false,message:'先に管理対象モデルを安全停止します。'};
const sandbox={document:{getElementById:id=>elements.get(id)},AbortSignal:{timeout:()=>undefined},fetch:async(url,options)=>{
 requests.push({url,options});
 if(url==='/api/resources/release'){
  if(blocked)return{ok:false,json:async()=>({detail:{message:'生成中または待機中です。'}})};
  state={...state,loaded_model:null,state:'STOPPED',ready_to_start:true,message:'sudo操作は不要です。'};
  return {ok:true,json:async()=>({state:'STOPPED'})};
 }
 return {ok:true,json:async()=>state};
}};
vm.runInNewContext(fs.readFileSync('frontend/resource-memory.js','utf8'),sandbox);
(async()=>{
 assert.ok(sandbox.resourceFailureMessage({reason:'gpu_allocation_failed',ram_available_mib:30000,swap_free_mib:0}).startsWith('GPUメモリ割り当て失敗'));
 assert.ok(sandbox.resourceFailureMessage({reason:'insufficient_ram',ram_available_mib:1000,swap_free_mib:0}).startsWith('RAM不足'));
 assert.equal(requests.length,0,'page load must not start any model or fetch private data');
 await elements.get('memory-refresh').events.click();
 const text=elements.get('memory-status').textContent;
 for(const word of ['RAM 利用可能 52000 MiB','Swap 使用 8192 MiB / 空き 0 MiB','VRAM 使用 9000 MiB','稼働モデル strata','先に管理対象モデル'])assert.ok(text.includes(word));
 await elements.get('memory-release').events.click();
 assert.deepEqual(JSON.parse(requests.find(r=>r.url==='/api/resources/release').options.body),{target:'strata',action:'stop'});
 assert.ok(elements.get('memory-status').textContent.includes('sudo操作は不要'));
 blocked=true;
 await elements.get('memory-stop').events.click();
 assert.equal(elements.get('memory-status').textContent,'生成中または待機中です。');
 assert.equal(elements.get('memory-stop').disabled,false);
 assert.ok(requests.every(r=>r.options.cache==='no-store'));
 assert.ok(requests.every(r=>r.url.startsWith('/api/resources/')));
 assert.ok(!requests.some(r=>r.url.includes('swap')));
 console.log('PASS numeric memory UI, owned release, busy refusal and no swap control');
})().catch(error=>{console.error(error);process.exitCode=1});
