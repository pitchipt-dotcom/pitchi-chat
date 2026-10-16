"""Pure synthetic official graph contract; no model or GPU."""
from backend.app.tools.flux_klein import flux_klein_graph


def test_distilled_workflow_uses_fixed_native_nodes_and_settings():
    graph=flux_klein_graph(model='synthetic.safetensors',text_encoder='synthetic_clip.safetensors',
        vae='synthetic_vae.safetensors',positive='A synthetic cat',seed=17,width=1024,height=1024)
    assert graph['positive']['inputs']['text']=='A synthetic cat'
    assert graph['encoder']['inputs']['type']=='flux2'
    assert graph['negative']=={'class_type':'ConditioningZeroOut','inputs':{'conditioning':['positive',0]}}
    assert graph['sigmas']['inputs']=={'steps':4,'width':1024,'height':1024}
    assert graph['guider']['inputs']['cfg']==1
    assert graph['sampler']['inputs']['sampler_name']=='euler'
    assert graph['noise']['inputs']['noise_seed']==17
    assert graph['latent']['inputs']=={'width':1024,'height':1024,'batch_size':1}
    assert graph['save']['class_type']=='SaveImage'
