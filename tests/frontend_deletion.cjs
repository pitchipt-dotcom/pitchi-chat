const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map(),requests=[];
class E {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.hidden=false;this.checked=false;}
  append(...x){this.children.push(...x);} replaceChildren(...x){this.children=x;}
  setAttribute(k,v){this[k]=v;} addEventListener(k,fn){this.events[k]=fn;}
  showModal(){this.open=true;} close(){this.open=false;}
}
const get=id=>{if(!nodes.has(id))nodes.set(id,new E());return nodes.get(id);};
const cid='11111111-1111-4111-8111-111111111111',image='22222222-2222-4222-8222-222222222222',operation='33333333-3333-4333-8333-333333333333';
let items=[{kind:'conversation',id:cid,label:'SYNTHETIC <script>',created_at:'fixture'},{kind:'image',id:image,label:'画像',created_at:'fixture'}],done=0;
const sandbox={document:{getElementById:get,createElement:t=>new E(t),createTextNode:t=>({textContent:t})},console};
vm.runInNewContext(fs.readFileSync('frontend/deletions.js','utf8'),sandbox);
const api=async(path,options)=>{requests.push({path,options});
 if(path.endsWith('/catalog'))return {items,pending:[]};
 if(path.endsWith('/preview'))return {operation_id:operation,scope:{conversations:1,images:1,jobs:0,files:2},warning:'元ファイルも削除',exclusions:['外部原本'],retained:[]};
 if(path.endsWith('/execute')){items=[];return {operation_id:operation,status:'COMPLETED',files_removed:2,targets:[{kind:'conversation',id:cid}],exclusions:['外部原本']};}
 throw Error(path);
};
(async()=>{
 const ui=sandbox.PitchDeletions({api,onDeleted:async()=>{done++;},isBusy:()=>false});await ui.open();
 assert.equal(get('deletion-items').children.length,2);
 await get('deletion-multiple').events.click();
 for(const row of get('deletion-items').children){const box=row.children[0].children[0];box.checked=true;box.events.change();}
 await get('deletion-selected').events.click();
 const targets=JSON.parse(requests.find(x=>x.path.endsWith('/preview')).options.body).targets;assert.equal(targets.length,2);assert.equal(targets[0].path,undefined);
 await get('deletion-execute').events.click();assert.equal(requests.filter(x=>x.path.endsWith('/execute')).length,0,'confirmation must be explicit');
 get('deletion-check').checked=true;await get('deletion-execute').events.click();assert.equal(done,1);assert.equal(get('deletion-items').children.length,0);
 assert.match(get('deletion-status').textContent,/外部原本/);assert.match(get('deletion-status').textContent,/完了/);
 const before=requests.length;ui.offline();assert.equal(requests.length,before,'no delete replay');assert.equal(get('deletion-items').children.length,0);
 console.log('Deletion multi-select, explicit confirmation, ID-only request, refresh and offline checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
