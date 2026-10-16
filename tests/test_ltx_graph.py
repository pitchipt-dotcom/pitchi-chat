"""Pure native LTX graph contracts; no external service, GPU or media file."""
import pytest
from backend.app.harness.image import ImageSpec, PreparedImagePrompt
from backend.app.tools.ltx_video import LTXVideoBackend, SIGMAS


def test_native_ltx_graph_and_single_reference_contract():
    from backend.app.image_assets import ImageReference
    from uuid import uuid4
    backend = LTXVideoBackend()
    spec = ImageSpec(purpose='video',subject='cat walks',style='realistic',composition='wide',aspect_ratio='16:9',text_requirement='')
    prepared = PreparedImagePrompt(positive='cat walks',seed=17,backend_sha256='a'*64)
    graph = backend.graph(spec,1,prepared)
    assert graph['8']['inputs']['sigmas'] == SIGMAS
    assert graph['9']['inputs']['cfg'] == 1 and graph['9']['inputs']['noise_seed'] == 17
    assert graph['6']['inputs'] == {'width':608,'height':352,'length':73,'batch_size':1}
    reference = ImageReference(id=uuid4(),sha256='a'*64,width=608,height=352)
    edit = backend.graph(spec,1,prepared,reference)
    assert '6' not in edit and edit['13']['inputs']['image']==reference.name
    assert edit['9']['inputs']['latent_image']==['14',2]
    with pytest.raises(ValueError): backend.graph(spec,2,prepared)
