"""Reviewed native LTX 2B distilled graph; fixed measured shape, no downloads."""
from typing import Literal

from backend.app.harness.contracts import Contract
from backend.app.tools.image_workflows import model_file

SIGMAS = '1.0000, 0.9937, 0.9875, 0.9812, 0.9750, 0.9094, 0.7250, 0.4219, 0'


class LTXVideoBackend(Contract):
    id: Literal['ltx-video'] = 'ltx-video'
    family: Literal['ltx-video'] = 'ltx-video'
    model: str = 'ltxv-2b-0.9.8-distilled-fp8.safetensors'
    text_encoder: str = 't5xxl_fp8_e4m3fn.safetensors'
    workflow: Literal['ltx2b_distilled_v1'] = 'ltx2b_distilled_v1'
    width: Literal[608] = 608
    height: Literal[352] = 352
    frames: Literal[73] = 73
    fps: Literal[24] = 24
    steps: Literal[8] = 8

    def graph(self, spec, count, prepared=None, reference=None):
        if count != 1:
            raise ValueError('One video per clip')
        model_file(self.model); model_file(self.text_encoder)
        def node(kind, **inputs):
            return {'class_type': kind, 'inputs': inputs}
        graph = {
            '1': node('CheckpointLoaderSimple', ckpt_name=self.model),
            '2': node('CLIPLoader', clip_name=self.text_encoder, type='ltxv', device='default'),
            '3': node('CLIPTextEncode', clip=['2', 0], text=prepared.positive if prepared else spec.subject),
            '4': node('CLIPTextEncode', clip=['2', 0], text=prepared.negative if prepared else ''),
            '5': node('LTXVConditioning', positive=['3', 0], negative=['4', 0], frame_rate=24.0),
            '6': node('EmptyLTXVLatentVideo', width=608, height=352, length=73, batch_size=1),
            '7': node('KSamplerSelect', sampler_name='euler'),
            '8': node('ManualSigmas', sigmas=SIGMAS),
            '9': node('SamplerCustom', model=['1', 0], add_noise=True,
                      noise_seed=prepared.seed if prepared else 0, cfg=1.0,
                      positive=['5', 0], negative=['5', 1], sampler=['7', 0], sigmas=['8', 0], latent_image=['6', 0]),
            '10': node('VAEDecodeTiled', samples=['9', 0], vae=['1', 2], tile_size=512,
                       overlap=64, temporal_size=32, temporal_overlap=8),
            '11': node('CreateVideo', images=['10', 0], fps=24.0),
            '12': node('SaveVideo', video=['11', 0], filename_prefix='video/pitch_chat_ltx', format='auto', codec='auto'),
        }
        if reference:
            graph['13'] = node('LoadImage', image=reference.name)
            graph['14'] = node('LTXVImgToVideo', positive=['5', 0], negative=['5', 1], vae=['1', 2],
                               image=['13', 0], width=608, height=352, length=73, batch_size=1, strength=1.0)
            graph['9']['inputs'].update(positive=['14', 0], negative=['14', 1], latent_image=['14', 2])
            del graph['6']
        return graph
