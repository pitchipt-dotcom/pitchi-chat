// Synthetic DOM/API checks. No live browser, model or GPU is used.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {scenario}=require('./frontend_models.cjs');
(async()=>{
 const submitted=[];let retries=0;
 const response=data=>({ok:true,status:200,json:async()=>data});
 const ui=await scenario([{id:'strata',label:'Strata',available:true}], 'strata',null,{status:'ok',storage:'ok'}, {
  respond:async(path,options)=>{
   if(path==='/api/images/backends')return response({generation_enabled:true,backends:[{id:'sdxl',status:'on_demand'}]});
   if(path==='/api/media/jobs'){
    submitted.push(JSON.parse(options.body));return response({job_id:'media-job',status:'QUEUED'});
   }
   if(path==='/api/jobs/media-job')return response({status:'SUCCESS',media_phase:'COMPLETED'});
   if(path==='/api/jobs/media-job/result')return response({image_spec:{media_kind:submitted.at(-1).kind||'image',positive_prompt:'moving cat',negative_prompt:'watermark',seed:17},tool_result:{artifacts:[{path:'/api/jobs/media-job/artifacts/0',kind:submitted.at(-1).kind==='video'?'file':'image'}]}});
   if(path==='/api/media/jobs/media-job/retry'){retries++;return response({job_id:'retry-child',status:'QUEUED'});}
   return null;
  }
 });
 ui.get('image-prompt-mode').value='direct';ui.get('image-prompt-mode').listeners.input();ui.get('image-request').value='cat';
 await ui.context.submitImage(false);await ui.context.pollImage();
 assert.equal(submitted[0].prompt_mode,'direct');
 assert.equal(ui.get('image-results').children[0].src,'/api/jobs/media-job/artifacts/0');
 assert.equal(ui.get('image-results').children[1].download,'generated-image.png');
 ui.get('video-request').value='cat turns its head';ui.get('video-prompt-mode').value='strata';
 ui.context.Event=class {constructor(type){this.type=type;}};
 ui.get('chat-mode').dispatchEvent=event=>ui.get('chat-mode').listeners[event.type]?.(event);
 await ui.get('video-form').listeners.submit({preventDefault(){}});
 assert.equal(submitted.length,1,'legacy entry must open the standard video mode without submitting');
 assert.equal(ui.get('chat-mode').value,'video');assert.equal(ui.get('message').value,'cat turns its head');
 assert.equal(ui.get('chat-video-prompt-mode').value,'strata');assert.equal(ui.get('management-menu').open,false);
 // Preserve rendering/retry compatibility for an already saved legacy H3 job.
 submitted.push({kind:'video',backend:'minimax-h3',prompt_mode:'strata'});
 ui.get('image-results').replaceChildren();
 vm.runInContext('imageJob="media-job"; imageIsMedia=true; imageCompiling=false;',ui.context);
 await ui.context.pollImage();
 assert.equal(ui.get('image-results').children[0].controls,true);
 assert.equal(ui.get('image-results').children[1].download,'generated-video.mp4');
 await ui.get('image-results').children[2].listeners.click();assert.equal(retries,1);
 assert.equal(submitted.length,2,'retry must use saved-plan child API');
 console.log('Media direct/video/preview/save/retry DOM checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
