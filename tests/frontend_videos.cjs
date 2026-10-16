// Isolated DOM/protocol, no GPU, model, private data or physical phone.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const nodes = new Map(), requests = [], timers = [];
class Element {
  constructor(tag='div') { this.tagName=tag; this.value=''; this.children=[]; this.events={}; this.style={}; }
  addEventListener(name,fn) { this.events[name]=fn; }
  append(...items) { for (const x of items) { this.children.push(x); if(x.id) nodes.set(x.id,x); } }
  add(item) { this.children.push(item); }
  replaceChildren(...items) { this.children=items; }
  setAttribute(key,value) { this[key]=value; }
  focus() {}
}
const get = id => { if(!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id); };
const cid='11111111-1111-4111-8111-111111111111', clip='22222222-2222-4222-8222-222222222222', project='33333333-3333-4333-8333-333333333333', image='44444444-4444-4444-8444-444444444444';
let complete=false, conversation=cid, queued=false, failed=false;
const sandbox={document:{getElementById:id=>nodes.get(id)||(id==='chat-video-timeline'?null:get(id)),createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text})},
  Option:function(text,value){this.text=text;this.value=value;},setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},console};
vm.runInNewContext(fs.readFileSync('frontend/chat-videos.js','utf8'),sandbox);
const api=async(path,options)=>{
  requests.push({path,options});
  if(path==='/api/media/backends') return {video:[{id:'ltx-video',status:'on_demand'},{id:'minimax-h3',status:'on_demand'}],default_video_backend:'ltx-video'};
  if(path.endsWith('/images'))return {images:[{id:image,source_kind:'video_frame',created_at:'synthetic'}]};
  if(path.endsWith('/videos'))return {clips:complete?[{clip_id:clip,project_id:project,status:failed?'ERROR':queued?'RUNNING':'SUCCESS',settings:failed?null:{frames:73},backend:'ltx-video',prompt_mode:'direct',prompt:'SYNTHETIC <script>never HTML</script>',start_frame:0,end_frame:73,video_url:`/api/jobs/${clip}/artifacts/0`}]:[],assemblies:[]};
  if(path.startsWith('/api/jobs/'))return {status:'RUNNING',media_phase:'GENERATING'};
  if(options?.method==='POST'){complete=true;return {job_id:clip,status:'QUEUED'};}
  throw Error(path);
};
(async()=>{
  const ui=sandbox.PitchVideos({api,getConversation:()=>conversation,setConversation:id=>{conversation=id;},refreshHistory:async()=>{},isBusy:()=>false});
  await ui.capabilities(); assert.equal(get('chat-video-backend').value,'ltx-video');
  get('chat-mode').value='video'; get('chat-video-prompt-mode').value='direct'; get('message').value='a cat walks';
  await ui.load(cid); assert.equal(nodes.has('chat-video-timeline'),false,'empty video section must not change message count');
  const event={preventDefault(){},stopImmediatePropagation(){}};
  await get('chat-form').events.submit(event);
  const root=JSON.parse(requests.find(x=>x.path==='/api/media/jobs').options.body);
  assert.equal(root.backend,'ltx-video');assert.equal(root.prompt_mode,'direct');assert.equal(root.conversation_id,cid);
  const card=get('chat-video-timeline').children[0], player=card.children.find(x=>x.tagName==='video');
  assert.equal(player.controls,true); assert.equal(player.playsInline,true); assert.equal(player.src,`/api/jobs/${clip}/artifacts/0`);
  assert.equal(card.children[1].textContent,'SYNTHETIC <script>never HTML</script>');
  get('message').value='continue'; get('chat-video-prompt-mode').value='qwen35';
  await card.children.find(x=>x.textContent==='続きを生成').events.click();
  assert.equal(JSON.parse(requests.find(x=>x.path.endsWith('/extend')).options.body).prompt_mode,'qwen35');
  for(const backend of ['ltx-video','minimax-h3']) for(const mode of ['direct','strata','qwen35']) {
    get('chat-video-backend').value=backend;get('chat-video-prompt-mode').value=mode;
    await get('chat-form').events.submit(event);
    const payload=JSON.parse(requests.filter(x=>x.path==='/api/media/jobs').at(-1).options.body);
    assert.equal(payload.backend,backend);assert.equal(payload.prompt_mode,mode);
  }
  failed=true;await ui.load(cid);assert.equal(get('chat-video-timeline').children[0].children.some(x=>x.textContent==='保存済み計画で再試行'),false,'no saved-plan retry is offered before a prompt exists');failed=false;
  const before=requests.filter(x=>x.options?.method==='POST').length;
  queued=true;await ui.load(cid);assert.equal(requests.filter(x=>x.options?.method==='POST').length,before,'reconnect uses GET only');
  ui.offline();conversation=null;await ui.load(null);
  const html=fs.readFileSync('frontend/index.html','utf8');assert.match(html,/video-prompt-mode/);assert.match(html,/chat-video-prompt-mode"><option value="direct"/);
  console.log('Video selection, inline playback, extension, safe DOM and reconnect checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
