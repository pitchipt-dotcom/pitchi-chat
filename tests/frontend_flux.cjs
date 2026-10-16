// Synthetic DOM/fetch verification, not a real browser/GPU trial.
const assert=require('node:assert/strict');
const {scenario}=require('./frontend_models.cjs');
(async()=>{
 let source=null,edited=null;
 const response=data=>({ok:true,status:200,json:async()=>data});
 const ui=await scenario([{id:'strata',label:'Strata',available:true}],'strata',null,{status:'ok',storage:'ok'},{respond:async(path,options)=>{
  if(path==='/api/images/backends')return response({generation_enabled:true,backends:[{id:'flux-klein',family:'flux2-klein',status:'on_demand'}]});
  if(path==='/api/media/jobs'){source=JSON.parse(options.body);return response({job_id:'preview',status:'QUEUED'});}
  if(path==='/api/jobs/preview')return response({status:'SUCCESS',media_phase:'PROMPT_SAVED'});
  if(path==='/api/jobs/preview/result')return response({image_spec:{positive_prompt:'Generated original cat',negative_prompt:'',backend:'flux-klein',seed:17}});
  if(path==='/api/media/jobs/preview/render'){edited=JSON.parse(options.body);return response({job_id:'render',status:'QUEUED'});}
  if(path==='/api/jobs/render')return response({status:'SUCCESS',media_phase:'COMPLETED'});
  if(path==='/api/jobs/render/result')return response({image_spec:{positive_prompt:edited.positive_prompt,negative_prompt:'',backend:'flux-klein',seed:17},tool_result:{artifacts:[{path:'/api/jobs/render/artifacts/0',kind:'image'}]}});
  return null;
 }});
 ui.get('image-prompt-mode').value='strata';ui.get('image-prompt-mode').listeners.input();ui.get('image-request').value='cat';
 assert.equal(ui.get('image-generate').disabled,true,'Strata FLUX must preview first');
 await ui.context.submitImage(true);await ui.context.pollImage();
 assert.equal(source.preview_only,true);assert.equal(source.backend,'flux-klein');
 assert.equal(ui.get('media-editor').hidden,false);assert.equal(ui.get('media-negative').disabled,true);
 assert.equal(ui.get('image-generate').disabled,false);
 ui.get('media-positive').value='Human edited cat with yellow flowers';
 await ui.context.submitImage(false);await ui.context.pollImage();
 assert.equal(edited.positive_prompt,'Human edited cat with yellow flowers');assert.equal(edited.negative_prompt,'');
 assert.equal(ui.get('image-results').children[0].src,'/api/jobs/render/artifacts/0');
 console.log('FLUX preview/edit/render DOM checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
