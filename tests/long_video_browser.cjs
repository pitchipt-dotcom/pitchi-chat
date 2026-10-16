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
 await cdp('Page.navigate',{url:origin});await until(()=>evaluate('typeof hostState!=="undefined" && hostState==="online" && !isBusy && !hydrationPromise'),'Host online');
 await evaluate(`document.getElementById('conversations').value=${JSON.stringify(fixtures.cid)};document.getElementById('conversations').dispatchEvent(new Event('change'));`);
 await until(()=>evaluate(`conversationId===${JSON.stringify(fixtures.cid)} && !isBusy`),'saved conversation');
 await evaluate(`document.getElementById('chat-mode').value='video';document.getElementById('chat-mode').dispatchEvent(new Event('change'));document.getElementById('chat-video-length-mode').value='long';document.getElementById('chat-video-length-mode').dispatchEvent(new Event('change'));document.getElementById('message').value='A red ball moves right.';document.getElementById('long-video-start').click();`);
 await until(async()=>{const data=await(await fetch(origin+'/api/conversations/'+fixtures.cid+'/long-videos')).json();return data.jobs[0]?.status==='AWAITING_ESTIMATE_APPROVAL';},'first-clip budget gate');
 await evaluate('longVideos.load(conversationId)');
 assert.match(await evaluate('document.getElementById("long-video-timeline").textContent'),/初回実測/);
 assert.equal(await evaluate('document.querySelectorAll(".long-video-card details video").length'),1,'first clip is playable before approval');
 assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth'),'mobile has no horizontal overflow');
 const parent=(await(await fetch(origin+'/api/conversations/'+fixtures.cid+'/long-videos')).json()).jobs[0];
 const approve=()=>evaluate(`(()=>{const card=document.querySelector('.long-video-card');card.querySelector('input[type=checkbox]').click();[...card.querySelectorAll('button')].find(x=>x.textContent==='見積もりを承認して残りを生成').click();})()`);
 await approve();
 await until(async()=>{const data=await(await fetch(origin+'/api/long-videos/'+parent.id)).json();return data.status==='RUNNING'||data.status==='COMPLETED';},'approve accepted');
 await cdp('Network.enable');await cdp('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:-1,uploadThroughput:-1});
 await cdp('Page.setWebLifecycleState',{state:'frozen'});
 await until(async()=>(await(await fetch(origin+'/api/long-videos/'+parent.id)).json()).status==='COMPLETED','Host completes offline');
 await cdp('Page.setWebLifecycleState',{state:'active'});await cdp('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
 await evaluate('checkHost()');
 await until(()=>evaluate('hostState==="online" && !isBusy && !hydrationPromise'),'reconnect hydration complete');
 await evaluate('longVideos.load(conversationId)');
 await until(()=>evaluate('!!document.querySelector(".long-video-card > video")'),'result recovered');
 const final=(await(await fetch(origin+'/api/long-videos/'+parent.id)).json());assert.equal(final.completed_clips,5);assert.equal(final.completed_seconds,15);
 await cdp('Page.bringToFront');await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
 await evaluate(`(()=>{const v=document.querySelector('.long-video-card > video');v.muted=true;return v.play();})()`);
 await until(()=>evaluate('(document.querySelector(".long-video-card > video")?.currentTime || 0)>0'),'inline playback');
 await cdp('Page.reload',{ignoreCache:true});await until(()=>evaluate('typeof hostState!=="undefined" && hostState==="online" && !isBusy && !hydrationPromise'),'reload online');
 await evaluate(`document.getElementById('conversations').value=${JSON.stringify(fixtures.cid)};document.getElementById('conversations').dispatchEvent(new Event('change'));`);
 await until(()=>evaluate('!!document.querySelector(".long-video-card > video")'),'history result reload');
 await evaluate(`globalThis.pitchDeleteItem('job',${JSON.stringify(final.final_job_id)})`);
 await until(()=>evaluate('!document.getElementById("deletion-confirmation").hidden'),'final-only deletion scope');
 await evaluate('document.getElementById("deletion-check").click();document.getElementById("deletion-execute").click()');
 await until(async()=>(await(await fetch(origin+'/api/long-videos/'+parent.id)).json()).status==='OUTPUT_DELETED','final deleted only');
 const kept=(await(await fetch(origin+'/api/long-videos/'+parent.id)).json());assert.equal(kept.completed_clips,5);
 await until(()=>evaluate('!document.getElementById("deletion-execute").disabled'),'deletion finished');
 await evaluate(`globalThis.pitchDeleteItem('long_video',${JSON.stringify(parent.id)})`);
 await until(()=>evaluate('!document.getElementById("deletion-confirmation").hidden'),'intermediate deletion scope');
 await evaluate('document.getElementById("deletion-check").click();document.getElementById("deletion-execute").click()');
 await until(async()=>(await fetch(origin+'/api/long-videos/'+parent.id)).status===410,'parent and intermediates deleted');
 assert.equal(errors.length,0,errors.join('\n'));
 console.log('PASS actual 390px Chrome/mock GPU: first-clip gate, offline completion, playback, reload, final-only and intermediate deletion');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{
 try{if(socket?.readyState===1)await cdp('Browser.close');}catch{}socket?.close();if(browser&&browser.exitCode===null)browser.kill('SIGTERM');
 for(const p of waiting.values())clearTimeout(p.timer);await pause(150);fs.rmSync(profile,{recursive:true,force:true});
});
