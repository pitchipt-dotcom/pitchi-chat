// DOM/fetch mocks; no LLM or GPU runs.
const assert = require('node:assert/strict');
const {scenario}=require('./frontend_models.cjs');
(async()=>{
 let previews=0;let previewSpec=null;let renderedSpec=null;
 const response=data=>({ok:true,status:200,json:async()=>data});
 const ui=await scenario([{id:'strata',label:'Strata',available:true}], 'strata',null,{status:'ok',storage:'ok'}, {
  respond:async(path,options)=>{
   if(path==='/api/images/backends')return response({generation_enabled:true,backends:[{id:'qwen-image',status:'compile_only'},{id:'sdxl',status:'on_demand'}]});
   if(path==='/api/images/jobs'){
    previews++;const body=JSON.parse(options.body);
    previewSpec={purpose:'poster',subject:'cat',style:'watercolor',composition:'centered',aspect_ratio:body.aspect_ratio,text_requirement:'',backend_preference:body.backend,negative_constraints:['blur'],quality_preference:'standard',reference_image_required:false};
    return response({job_id:`preview-${previews}`,status:'QUEUED'});
   }
   if(/^\/api\/jobs\/preview-\d+$/.test(path))return response({status:'SUCCESS',image_phase:'SPEC_READY'});
   if(/^\/api\/jobs\/preview-\d+\/result$/.test(path))return response({image_spec:previewSpec});
   if(/^\/api\/images\/jobs\/preview-\d+\/render$/.test(path)){
    renderedSpec=JSON.parse(options.body);return response({job_id:'rendered',status:'QUEUED'});
   }
   if(path==='/api/jobs/rendered')return response({status:'SUCCESS',image_phase:'GENERATING'});
   if(path==='/api/jobs/rendered/result')return response({image_spec:renderedSpec,tool_result:{artifacts:[{path:'/api/jobs/rendered/artifacts/0'}]}});
   return null;
  }
 });
 assert.equal(ui.get('image-backend').value,'sdxl');
 assert.equal(ui.get('image-generate').disabled,true);
 ui.get('image-request').value='cat image';
 await ui.context.submitImage(true);await ui.context.pollImage();
 assert.equal(ui.get('image-editor').hidden,false);
 assert.equal(ui.get('image-generate').disabled,false);
 ui.get('image-subject').value='USER EDITED CAT';
 await ui.context.submitImage(false);await ui.context.pollImage();
 assert.equal(previews,1,'render must not recompile reviewed edits');
 assert.equal(renderedSpec.subject,'USER EDITED CAT');
 assert.equal(renderedSpec.aspect_ratio,'1:1');
 assert.equal(ui.get('image-results').children[0].src,'/api/jobs/rendered/artifacts/0');
 ui.get('image-backend').value='qwen-image';ui.get('image-backend').listeners.input();
 await ui.context.submitImage(true);await ui.context.pollImage();
 assert.equal(ui.get('image-generate').disabled,true,'unverified Qwen rendering remains blocked');
 assert.equal(ui.get('image-editor').hidden,false,'Qwen prompt preview remains editable');
 console.log('Image preview/edit/render DOM checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
