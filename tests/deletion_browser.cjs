// Actual Chrome/DOM against an isolated real Host and synthetic media only.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const [chrome,origin,fixtureFile]=process.argv.slice(2), fixtures=JSON.parse(fs.readFileSync(fixtureFile,'utf8'));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'pitch-delete-browser-'));
let browser,socket,sequence=0;const waiting=new Map(),errors=[];
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn,label){const deadline=Date.now()+15000;while(Date.now()<deadline){if(await fn())return;await pause(60);}throw Error('Timeout: '+label);}
function cdp(method,params={}){const id=++sequence;return new Promise((resolve,reject)=>{
 const timer=setTimeout(()=>{waiting.delete(id);reject(Error(method+' timeout'));},15000);waiting.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;}
async function catalog(){return (await (await fetch(origin+'/api/deletions/catalog')).json()).items;}
(async()=>{
 browser=spawn(chrome,['--headless=new','--disable-gpu','--no-sandbox','--no-first-run','--no-default-browser-check','--no-proxy-server','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
 await until(()=>fs.existsSync(path.join(profile,'DevToolsActivePort')),'Chrome');
 const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();socket=new WebSocket(targets.find(x=>x.type==='page').webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
 socket.onmessage=event=>{const r=JSON.parse(event.data);if(r.method==='Runtime.exceptionThrown')errors.push(r.params.exceptionDetails.exception?.description||r.params.exceptionDetails.text);const p=waiting.get(r.id);if(!p)return;clearTimeout(p.timer);waiting.delete(r.id);r.error?p.reject(Error(r.error.message)):p.resolve(r.result);};
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await cdp('Page.navigate',{url:origin});await until(()=>evaluate('typeof hostState!=="undefined" && hostState==="online" && !isBusy'),'Host online');
 await evaluate('document.getElementById("conversation-menu").open=true; document.getElementById("deletion-open").click()');
 await until(()=>evaluate('document.querySelectorAll(".deletion-item").length>=4'),'deletion catalog');
 assert.equal(await evaluate('document.getElementById("deletion-dialog").open'),true);
 assert.ok(await evaluate('document.getElementById("deletion-dialog").getBoundingClientRect().width<=innerWidth'),'mobile dialog width');
 const first=fixtures.images[0];
 await evaluate(`(()=>{const row=[...document.querySelectorAll('.deletion-item')].find(x=>x.textContent.includes(${JSON.stringify(first.slice(0,8))}));row.querySelector('details').open=true;row.querySelector('button').click();})()`);
 await until(()=>evaluate('!document.getElementById("deletion-confirmation").hidden'),'scope confirmation');
 assert.match(await evaluate('document.getElementById("deletion-scope").textContent'),/元ファイル/);
 await evaluate('document.getElementById("deletion-execute").click()');await pause(100);
 assert.ok((await catalog()).some(x=>x.id===first),'unchecked confirmation never deletes');
 await evaluate('document.getElementById("deletion-check").click();document.getElementById("deletion-execute").click()');
 await until(async()=>!(await catalog()).some(x=>x.id===first),'physical image delete');
 assert.equal((await fetch(origin+`/api/images/attachments/${first}/content`)).status,410);
 await until(()=>evaluate('!document.getElementById("deletion-execute").disabled'),'first deletion complete');
 await evaluate('document.getElementById("deletion-multiple").click()');
 await evaluate(`(()=>{for(const row of document.querySelectorAll('.deletion-item')) if(${JSON.stringify(fixtures.batch.map(x=>x.slice(0,8)))}.some(id=>row.textContent.includes(id))) row.querySelector('input').click();document.getElementById('deletion-selected').click();})()`);
 await until(()=>evaluate('!document.getElementById("deletion-confirmation").hidden'),'batch preview');
 await evaluate('document.getElementById("deletion-check").click();document.getElementById("deletion-execute").click()');
 await until(async()=>!(await catalog()).some(x=>fixtures.batch.includes(x.id)),'batch deleted');
 await cdp('Page.reload',{ignoreCache:true});await until(()=>evaluate('typeof hostState!=="undefined" && hostState==="online" && !isBusy'),'reloaded');
 await evaluate('document.getElementById("deletion-open").click()');await until(()=>evaluate('document.getElementById("deletion-items").children.length>0'),'remaining catalog');
 const remaining=await catalog();assert.ok(remaining.some(x=>x.id===fixtures.keep));assert.ok(!remaining.some(x=>[first,...fixtures.batch].includes(x.id)));
 assert.equal(errors.length,0,errors.join('\n'));
 console.log('PASS actual 390px Chrome: item menu, original-file confirmation, batch deletion, reload, denied file access, unrelated conversation retained');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{
 try{if(socket?.readyState===1)await cdp('Browser.close');}catch{}socket?.close();if(browser&&browser.exitCode===null)browser.kill('SIGTERM');
 for(const p of waiting.values())clearTimeout(p.timer);await pause(150);fs.rmSync(profile,{recursive:true,force:true});
});
