"""Bounded CPU clip normalization/assembly; no GPU, shell or client paths."""
import asyncio
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from backend.app.harness.contracts import Artifact, ExecutionResult, FinalStatus, ToolResult
from backend.app.harness.policy import authorized_path
from backend.app.tools.comfy_video import checked_process, validate_clip


async def cpu_process(*args,timeout=30):
    if not shutil.which('prlimit'):
        raise ValueError('bounded_cpu_process_unavailable')
    return await checked_process('prlimit','--as=2147483648','--',*args,timeout=timeout)


@contextmanager
def staging(root, record, forget):
    paths=[]
    def reserve(suffix):
        path=Path(root)/(uuid4().hex+suffix)
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(descriptor)
        try:
            if record:record(path.name)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        paths.append(path)
        return path
    try:yield reserve
    finally:
        for path in paths:
            if path.is_symlink():continue
            path.unlink(missing_ok=True)
            if forget:forget(path.name)


def clip_count(seconds, frames, fade=0):
    # Repeated I2V first frames and optional overlaps do not add to final length.
    target = seconds*24
    stride = frames-1-fade
    return 1+max(0,(target-frames+stride-1)//stride)


async def frame(path, last=False):
    args = ['ffmpeg','-v','error']
    if last: args += ['-sseof','-0.05']
    return await checked_process(*args,'-i',str(path),'-frames:v','1','-f','image2pipe','-vcodec','png','-')


def frame_difference(first, second):
    import io
    from PIL import Image, ImageChops, ImageStat
    a=Image.open(io.BytesIO(first)).convert('RGB').resize((64,64))
    b=Image.open(io.BytesIO(second)).convert('RGB').resize((64,64))
    return round(sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/3,3)


async def assemble(paths, roots, output_root, *, seconds, aspect, fade=0, expected_hashes, record_temp=None, forget_temp=None):
    if (not 1<=len(paths)<=64 or seconds not in {15,30,60,90,120,180}
            or aspect not in {'16:9','9:16'} or fade not in {0,3} or len(paths)!=len(expected_hashes)):
        raise ValueError('invalid_long_assembly')
    # Do not consume swap as CPU assembly capacity. Existing GPU gates stay intact.
    from backend.app.resources.memory import memory_state
    reading=memory_state()
    if reading['ram_available_mib'] < 4096:
        raise ValueError('assembly_ram_reserve')
    output_root=Path(output_root);output_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    verified=[];infos=[]
    for value,expected in zip(paths,expected_hashes):
        path=authorized_path(str(value),roots)
        if path.is_symlink() or not path.is_file() or path.stat().st_size>64*1024*1024:
            raise ValueError('invalid_long_clip')
        if await asyncio.to_thread(lambda p=path:hashlib.sha256(p.read_bytes()).hexdigest())!=expected:
            raise ValueError('long_clip_changed')
        info=json.loads(await cpu_process('ffprobe','-v','error','-show_streams','-of','json',str(path)))
        infos.append(info);verified.append(path)
    required=max(512*1024*1024,sum(p.stat().st_size for p in verified)*4)
    if shutil.disk_usage(output_root).free<required:
        raise ValueError('assembly_disk_reserve')
    has_audio=any(any(s['codec_type']=='audio' for s in info['streams']) for info in infos)
    width,height=(576,324) if aspect=='16:9' else (324,576)
    dest=output_root/(uuid4().hex+'.mp4')
    fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    try:
        if record_temp:record_temp(dest.name)
        with staging(output_root,record_temp,forget_temp) as reserve:
            normalized=[];durations=[]
            for n,(path,info) in enumerate(zip(verified,infos)):
                out=reserve('.mp4');normalized.append(out)
                video=next(s for s in info['streams'] if s['codec_type']=='video')
                frames=int(video.get('nb_frames') or 0)
                if frames not in {73,90}:raise ValueError('unexpected_long_clip_shape')
                drop=1 if n else 0;durations.append((frames-drop)/24)
                command=['ffmpeg','-v','error','-xerror','-threads','1','-filter_threads','1','-i',str(path)]
                own_audio=any(s['codec_type']=='audio' for s in info['streams'])
                if has_audio and not own_audio:command+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
                command+=['-map','0:v:0','-vf',f'trim=start_frame={drop},setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=24,format=yuv420p',
                    '-c:v','libx264','-preset','veryfast','-crf','20','-threads','1','-t',str((frames-drop)/24)]
                if has_audio:
                    command+=['-map','0:a:0' if own_audio else '1:a:0','-af',f'atrim=start={drop/24},asetpts=PTS-STARTPTS,aresample=48000',
                        '-c:a','aac','-ar','48000','-ac','2']
                else:command+=['-an']
                await cpu_process(*command,'-y',str(out),timeout=60)
            if fade:
                command=['ffmpeg','-v','error','-threads','1','-filter_complex_threads','1']
                for path in normalized:command+=['-threads','1','-i',str(path)]
                filters=[];elapsed=durations[0];v='0:v';a='0:a'
                for n in range(1,len(normalized)):
                    filters.append(f'[{v}][{n}:v]xfade=transition=fade:duration={fade/24}:offset={elapsed-fade/24:.9f}[v{n}]')
                    if has_audio:filters.append(f'[{a}][{n}:a]acrossfade=d={fade/24}[a{n}]');a=f'a{n}'
                    v=f'v{n}';elapsed+=durations[n]-fade/24
                if filters:command+=['-filter_complex',';'.join(filters),'-map',f'[{v}]']
                else:command+=['-map','0:v:0']
                if has_audio:command+=['-map',f'[{a}]' if len(normalized)>1 else '0:a:0','-c:a','aac']
            else:
                manifest=reserve('.txt')
                manifest.write_text(''.join("file '"+str(p).replace("'","'\\''")+"'\n" for p in normalized))
                command=['ffmpeg','-v','error','-threads','1','-f','concat','-safe','0','-i',str(manifest),'-map','0:v:0']
                if has_audio:command+=['-map','0:a:0','-c:a','aac']
            command+=['-frames:v',str(seconds*24),'-t',str(seconds),'-r','24','-c:v','libx264','-preset','veryfast','-crf','20','-threads','1','-pix_fmt','yuv420p','-movflags','+faststart']
            if not has_audio:command+=['-an']
            await cpu_process(*command,'-y',str(dest),timeout=300)
            if dest.stat().st_size>256*1024*1024:raise ValueError('long_output_size_limit')
            await validate_clip(dest,width=width,height=height,frames=seconds*24,require_audio=has_audio)
            os.chmod(dest,0o600)
        return ExecutionResult(request_id=uuid4(),status=FinalStatus.SUCCESS,
            image_spec={'media_kind':'long_video','seconds':seconds,'aspect_ratio':aspect,'width':width,'height':height,
                'fps':24,'audio':has_audio,'crossfade_frames':fade,'duplicate_first_frames_removed':len(paths)-1},
            tool_result=ToolResult(success=True,result_type='run',run_id=uuid4().hex,run_status='completed',http_status=200,
                output={'artifact_count':1,'status':'completed'},artifacts=(Artifact(path=str(dest),kind='file'),)))
    except BaseException:
        dest.unlink(missing_ok=True)
        if forget_temp:forget_temp(dest.name)
        raise
