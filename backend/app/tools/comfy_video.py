"""Opt-in video through the existing owned ComfyUI profile and pinned UI workflow.

No external switcher lifecycle routes are called. The reviewed workflow is read
only; graph links/widgets are reused with just prompt, seed and output prefix.
"""
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import PrivateAttr

from backend.app.harness.contracts import Artifact, Contract, ErrorCode
from backend.app.harness.recovery import HarnessFailure
from backend.app.tools.comfyui import ComfyImageAdapter
from backend.app.tools.image_workflows import ImageWorkflowRegistry, model_file


WIDGETS = {
    'UNETLoader': ('unet_name', 'weight_dtype'),
    'CLIPLoader': ('clip_name', 'type', 'device'),
    'VAELoader': ('vae_name',),
    'MiniMaxH3ImageToVideo': ('prompt', 'width', 'height', 'length'),
    'MiniMaxH3SigmaShift': ('shift_video', 'shift_audio'),
    'RandomNoise': ('noise_seed',),
    'KSamplerSelect': ('sampler_name',),
    'BasicScheduler': ('scheduler', 'steps', 'denoise'),
    'BasicGuider': (), 'SamplerCustomAdvanced': (), 'VAEDecode': (),
    'VAEDecodeAudio': (), 'CreateVideo': ('fps',),
    'SaveVideo': ('filename_prefix', 'format', 'codec'),
}
EXPECTED = ('UNETLoader', 'CLIPLoader', 'VAELoader', 'VAELoader', 'MiniMaxH3ImageToVideo',
            'MiniMaxH3SigmaShift', 'RandomNoise', 'KSamplerSelect', 'BasicScheduler',
            'BasicGuider', 'SamplerCustomAdvanced', 'VAEDecode', 'VAEDecodeAudio', 'CreateVideo', 'SaveVideo')


class VideoBackend(Contract):
    id: Literal['minimax-h3'] = 'minimax-h3'
    family: Literal['minimax-h3-video'] = 'minimax-h3-video'
    model: str
    workflow_sha256: str
    width: Literal[608] = 608
    height: Literal[352] = 352
    frames: Literal[90] = 90
    fps: Literal[24] = 24
    steps: Literal[20] = 20
    _workflow: dict = PrivateAttr(default_factory=dict)

    def graph(self, spec, count, prepared=None, reference=None):
        if count != 1 or not self._workflow:
            raise ValueError('Video requires one reviewed workflow')
        nodes = {n['id']: n for n in self._workflow['nodes']}
        links = {v[0]: v for v in self._workflow['links']}
        graph = {}
        for index, kind in enumerate(EXPECTED, 1):
            node = nodes[index]
            values = node['widgets_values']
            inputs = dict(zip(WIDGETS[kind], values))
            for item in node['inputs']:
                if item.get('link') is not None:
                    edge = links[item['link']]
                    if edge[3] != index:
                        raise ValueError('Invalid workflow link')
                    inputs[item['name']] = [str(edge[1]), edge[2]]
            graph[str(index)] = {'class_type': kind, 'inputs': inputs}
        prompt = prepared.positive if prepared else spec.subject
        if prepared and prepared.negative:
            # This existing BasicGuider has no negative-conditioning input.
            prompt += '\nAvoid: ' + prepared.negative
        graph['5']['inputs']['prompt'] = prompt
        graph['7']['inputs']['noise_seed'] = prepared.seed if prepared else 0
        graph['15']['inputs']['filename_prefix'] = 'video/pitch_chat'
        if reference:
            graph['16'] = {'class_type': 'LoadImage', 'inputs': {'image': reference.name}}
            graph['5']['inputs']['first_frame'] = ['16', 0]
        return graph


def load_video_workflows():
    config = os.environ.get('PITCH_CHAT_VIDEO_WORKFLOW_CONFIG')
    if not config:
        return ImageWorkflowRegistry()
    settings = json.loads(Path(config).read_text())
    if set(settings) - {'workflow_path', 'workflow_sha256', 'ltx'} or not {'workflow_path', 'workflow_sha256'} <= set(settings):
        raise ValueError('Invalid video workflow configuration')
    data = Path(settings['workflow_path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != settings['workflow_sha256']:
        raise ValueError('Video workflow source changed')
    workflow = json.loads(data)
    nodes = {n['id']: n for n in workflow['nodes']}
    if set(nodes) != set(range(1, 16)) or any(nodes[i]['type'] != kind for i, kind in enumerate(EXPECTED, 1)):
        raise ValueError('Unreviewed video workflow')
    if (nodes[5]['widgets_values'][1:] != [608,352,90]
            or nodes[9]['widgets_values'] != ['beta',20,1] or nodes[14]['widgets_values'][0] != 24):
        raise ValueError('Unreviewed video dimensions/sampling')
    for i in (1,2,3,4):
        model_file(nodes[i]['widgets_values'][0])
    backend = VideoBackend(model=nodes[1]['widgets_values'][0], workflow_sha256=settings['workflow_sha256'])
    backend._workflow = workflow
    backends = [backend]
    if 'ltx' in settings:
        from backend.app.tools.ltx_video import LTXVideoBackend
        backends.insert(0, LTXVideoBackend.model_validate(settings['ltx']))
    return ImageWorkflowRegistry(tuple(backends))


async def checked_process(*args, timeout=30):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError('Video decode failed')
    return stdout


async def validate_clip(path, *, width=608, height=352, frames=90, fps=24, require_audio=True):
    raw = await checked_process('ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', str(path))
    info = json.loads(raw)
    video = [s for s in info['streams'] if s['codec_type'] == 'video']
    if len(video) != 1 or (require_audio and not any(s['codec_type']=='audio' for s in info['streams'])):
        raise ValueError('Video/audio streams missing')
    v = video[0]
    from fractions import Fraction
    if ((v['width'],v['height'],int(v['nb_read_frames'])) != (width,height,frames)
            or Fraction(v['avg_frame_rate']) != fps
            or abs(float(v.get('duration', info['format']['duration'])) - frames/fps) > .1):
        raise ValueError('Unexpected video dimensions/duration')
    # Decode every frame, including the audio stream, rather than accepting a header.
    await checked_process('ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-')
    return {'width':width, 'height':height, 'frames':frames, 'fps':fps, 'duration':float(v.get('duration', info['format']['duration']))}


async def validate_video(path):
    return await validate_clip(path)


class ComfyVideoAdapter(ComfyImageAdapter):
    save_node = '15'
    result_type = 'run'
    reference_families = frozenset({'ltx-video', 'minimax-h3-video'})

    def output_node(self, graph):
        nodes = [key for key, value in graph.items() if value['class_type'] == 'SaveVideo']
        if len(nodes) != 1:
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        return nodes[0]

    async def _artifact(self, image, graph):
        params = self._output_params(image, '.mp4')
        _, mime, data = await self.http.request('GET', '/view', params=params, max_bytes=64*1024*1024)
        if mime != 'video/mp4' or len(data)<32 or data[4:8] != b'ftyp':
            raise HarnessFailure(ErrorCode.RESULT_INVALID)
        try:
            with tempfile.TemporaryDirectory(prefix='pitch-media-') as folder:
                path = Path(folder)/'video.mp4'; path.write_bytes(data)
                if any(n['class_type'] == 'LTXVConditioning' for n in graph.values()):
                    await validate_clip(path, frames=73, require_audio=False)
                else:
                    await validate_video(path)
        except (ValueError, KeyError, TypeError, OSError, TimeoutError):
            raise HarnessFailure(ErrorCode.RESULT_INVALID) from None
        return Artifact(path=self.artifacts.save(data, '.mp4'), kind='file')
