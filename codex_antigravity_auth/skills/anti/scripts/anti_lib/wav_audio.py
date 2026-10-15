"""Bounded classic PCM WAV transport; forwarding is not verified listening."""
from __future__ import annotations

import array
import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
from types import MappingProxyType
from pathlib import Path
import re
import struct
import sys
import wave

from .media import MediaError, Session as MediaSession
from .inventory import read_file

MAX_FILES=2
MAX_FILE_BYTES=2*1024*1024
MAX_TOTAL_BYTES=4*1024*1024
MAX_SECONDS=30
MAX_REQUEST_BYTES=8*1024*1024
RATES={8000,16000,22050,24000,32000,44100,48000}
PART_TYPE='antigravity_audio'
SHA=re.compile(r'[0-9a-f]{64}')
FIELDS={'index','mime','bytes','duration_ms','sample_rate','channels','sample_width_bits','frames','sha256'}
WINDOW_MS=100
QUIET_PEAK_DBFS=-60.0
MAX_SEGMENTS=256
MEASUREMENT_BASIS='local_deterministic_pcm16'
SILENCE_POLICIES=('sent','skipped','sent_by_explicit_request')
WINDOW_STATES=('zero','quiet','sound')


class AudioError(MediaError, ValueError):
    pass


def require(condition, message):
    if not condition:raise AudioError(message)


def inspect_wav(raw):
    require(isinstance(raw,bytes) and 44<=len(raw)<=MAX_FILE_BYTES,'Audio must be a nonempty PCM WAV file at most2MiB')
    require(raw[:4]==b'RIFF' and raw[8:12]==b'WAVE','Audio requires classic RIFF/WAVE; no conversion is performed')
    require(struct.unpack_from('<I',raw,4)[0]+8==len(raw),'WAV RIFF length does not match captured bytes')
    offset=12;fmt=None;data=None
    while offset<len(raw):
        require(offset+8<=len(raw),'Truncated WAV chunk header')
        kind=raw[offset:offset+4];size=struct.unpack_from('<I',raw,offset+4)[0]
        start=offset+8;end=start+size
        require(end+(size%2)<=len(raw),'Truncated WAV chunk')
        if kind==b'fmt ':
            require(fmt is None and data is None and size in {16,18},'WAV requires one classic PCM format chunk before its data')
            fmt=struct.unpack_from('<HHIIHH',raw,start)
            require(size==16 or raw[start+16:start+18]==b'\0\0','Extended WAV formats are unsupported')
        elif kind==b'data':
            require(data is None and fmt is not None,'WAV requires one data chunk after its format')
            data=raw[start:end]
        offset=end+(size%2)
    require(fmt is not None and data is not None,'WAV format/data chunks are required')
    encoding,channels,rate,byte_rate,alignment,bits=fmt
    require(encoding==1 and bits==16 and channels in {1,2} and rate in RATES,
            'Audio requires16-bit PCM, mono/stereo, at8–48kHz supported sample rates')
    require(alignment==channels*2 and byte_rate==rate*alignment,'WAV byte rate or block alignment is inconsistent')
    require(len(data)>0 and len(data)%alignment==0,'WAV audio frames must be nonempty and complete')
    frames=len(data)//alignment
    require(frames<=rate*MAX_SECONDS,'Audio exceeds30seconds per file')
    try:
        with wave.open(io.BytesIO(raw),'rb') as source:
            require(source.getparams()[:4]==(channels,2,rate,frames),'WAV parser and chunk metadata disagree')
            require(source.readframes(frames)==data,'WAV frames differ from the captured data chunk')
    except (wave.Error,EOFError) as exc:
        raise AudioError('WAV could not be decoded as classic PCM') from exc
    return {'mime':'audio/wav','bytes':len(raw),'duration_ms':(frames*1000+rate-1)//rate,
            'sample_rate':rate,'channels':channels,'sample_width_bits':16,'frames':frames,
            'sha256':hashlib.sha256(raw).hexdigest()}


def _dbfs(level):
    return round(20*math.log10(level/32768),2) if level>0 else None


def measure_pcm(raw):
    with wave.open(io.BytesIO(raw),'rb') as source:
        channels,rate,frames=source.getnchannels(),source.getframerate(),source.getnframes()
        require(source.getsampwidth()==2 and frames>0,'Silence measurement requires nonempty 16-bit PCM frames')
        samples=array.array('h',source.readframes(frames))
    if sys.byteorder=='big':samples.byteswap()
    window=max(1,rate*WINDOW_MS//1000)
    quiet=32768*10**(QUIET_PEAK_DBFS/20)
    counts=dict.fromkeys(WINDOW_STATES,0);segments=[];peak=0;energy=0
    for first in range(0,frames,window):
        last=min(frames,first+window)
        chunk=samples[first*channels:last*channels]
        top=max(max(chunk),-min(chunk))
        peak=max(peak,top);energy+=sum(value*value for value in chunk)
        state='zero' if top==0 else 'quiet' if top<quiet else 'sound'
        counts[state]+=1
        if segments and segments[-1][2]==state:segments[-1][1]=last*1000//rate
        else:segments.append([first*1000//rate,last*1000//rate,state])
    return {'window_ms':WINDOW_MS,'quiet_peak_dbfs':QUIET_PEAK_DBFS,'wholly_digital_silence':peak==0,
            'peak_dbfs':_dbfs(peak),'rms_dbfs':_dbfs(math.sqrt(energy/len(samples))),'windows':counts,
            'segments':segments[:MAX_SEGMENTS],'segments_omitted':max(0,len(segments)-MAX_SEGMENTS)}


def decode_part(part):
    require(isinstance(part,dict) and set(part)=={'type','mime_type','data','probe_unverified'},'Invalid gateway audio-extension fields')
    require(part['type']==PART_TYPE and part['mime_type']=='audio/wav','Audio extension requires MIME audio/wav')
    require(part['probe_unverified'] is True,'Audio backend acceptance is unverified; explicit probe upload intent is required')
    encoded=part['data']
    require(isinstance(encoded,str) and 0<len(encoded)<=4*((MAX_FILE_BYTES+2)//3),'Audio base64 is missing or exceeds2MiB')
    try:raw=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error) as exc:raise AudioError('Invalid WAV base64') from exc
    require(base64.b64encode(raw).decode('ascii')==encoded,'Audio base64 must be canonical')
    return raw,inspect_wav(raw)


def parts(request):
    value=request.get('input') if isinstance(request,dict) else None
    found=[]
    if isinstance(value,list):
        for item in value:
            if isinstance(item,dict) and isinstance(item.get('content'),list):
                found.extend(part for part in item['content'] if isinstance(part,dict) and part.get('type')==PART_TYPE)
    return found


def validate_request(request, *, supported):
    found=parts(request)
    if not found:return
    require(isinstance(request.get('model'),str) and bool(request['model']),'Audio requires an explicit model')
    require(supported,'This route does not implement the experimental WAV input contract')
    require(request.get('stream',False) is False,'WAV input currently requires non-streaming generation')
    require(1<=len(found)<=MAX_FILES,'A WAV request accepts at most two files')
    total=0
    for part in found:
        raw,_info=decode_part(part);total+=len(raw)
    require(total<=MAX_TOTAL_BYTES,'Audio exceeds4MiB total')
    for item in request['input']:
        content=item.get('content') if isinstance(item,dict) else None
        if isinstance(content,list):
            if any(isinstance(p,dict) and p.get('type')==PART_TYPE for p in content):
                require(item.get('role','user')=='user','Audio is supported only in user messages')
            require(not any(isinstance(p,dict) and p.get('type') in {'image','input_image','input_audio','audio','input_video','video'} for p in content),
                    'Mixed media formats are unsupported in WAV probes')
    try:
        size=len(json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8'))
    except (ValueError,UnicodeError,TypeError) as exc:
        raise AudioError('Audio request must be finite UTF-8 JSON') from exc
    require(size<=MAX_REQUEST_BYTES,'Audio request exceeds8MiB including prompt and attachments')


def valid_identity(items):
    if not isinstance(items,list) or not 1<=len(items)<=MAX_FILES:return False
    for index,item in enumerate(items,1):
        if (not isinstance(item,dict) or set(item)!=FIELDS or type(item['index']) is not int or item['index']!=index
                or item['mime']!='audio/wav' or type(item['bytes']) is not int or not 44<=item['bytes']<=MAX_FILE_BYTES
                or type(item['sample_rate']) is not int or item['sample_rate'] not in RATES
                or type(item['channels']) is not int or item['channels'] not in {1,2}
                or type(item['sample_width_bits']) is not int or item['sample_width_bits']!=16
                or type(item['frames']) is not int or not 0<item['frames']<=item['sample_rate']*MAX_SECONDS
                or item['frames']*item['channels']*2>item['bytes']-44
                or type(item['duration_ms']) is not int or item['duration_ms']!=(item['frames']*1000+item['sample_rate']-1)//item['sample_rate']
                or not isinstance(item['sha256'],str) or not SHA.fullmatch(item['sha256'])):return False
    return sum(item['bytes'] for item in items)<=MAX_TOTAL_BYTES


@dataclass(frozen=True)
class Audio:
    descriptor: dict
    encoded: str=field(repr=False)

    def __post_init__(self):
        object.__setattr__(self,'descriptor',MappingProxyType(dict(self.descriptor)))

    @property
    def size(self):return self.descriptor['bytes']

    @property
    def is_digital_silence(self):
        with wave.open(io.BytesIO(base64.b64decode(self.encoded, validate=True)), 'rb') as source:
            return not any(source.readframes(source.getnframes()))

    def identity(self):return dict(self.descriptor)

    def part(self):
        return {'type':PART_TYPE,'mime_type':'audio/wav','data':self.encoded,'probe_unverified':True}


def capture(paths, *, policy=None, probe=False, single_backend_attempt=False, silence_policy='sent'):
    require(isinstance(paths,(list,tuple)) and 1<=len(paths)<=MAX_FILES,'Select one or two explicit local WAV files')
    selected=[]
    for value in paths:
        require(isinstance(value,str) and bool(value) and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://',value)
                and not value.lower().startswith('data:') and all(ord(c)>=32 and not 127<=ord(c)<160 for c in value),
                'Audio inputs must be explicit local paths, not URLs or inline data')
        try:
            value.encode('utf-8');selected.append(Path(value).expanduser().absolute())
        except (ValueError,OSError,UnicodeError) as exc:raise AudioError('Invalid audio path') from exc
    if policy is not None:policy.check_paths(selected,root=policy.root)
    attachments=[];remaining=MAX_TOTAL_BYTES
    for index,path in enumerate(selected,1):
        root=Path(path.anchor)
        raw,_size,reason=read_file(root,path.relative_to(root).as_posix(),min(MAX_FILE_BYTES,remaining))
        require(raw is not None and reason is None,'Audio capture refused; use regular no-follow files within2MiB each/4MiB total')
        descriptor={'index':index,**inspect_wav(raw)};remaining-=len(raw)
        attachments.append(Audio(descriptor,base64.b64encode(raw).decode('ascii')))
    return Session(tuple(attachments),probe=probe,single_backend_attempt=single_backend_attempt,
                   silence_policy=silence_policy)


class Session(MediaSession):
    kind='audio'

    def __init__(self,attachments,*,probe,single_backend_attempt=False,silence_policy='sent'):
        super().__init__(attachments)
        require(silence_policy in SILENCE_POLICIES,'Unknown digital-silence policy')
        self.probe=probe is True
        self.single_backend_attempt=single_backend_attempt is True
        self.silence_policy=silence_policy
        self.measurements=tuple(measure_pcm(base64.b64decode(item.encoded,validate=True)) for item in attachments)
        self.sent=tuple(silence_policy!='skipped' or not value['wholly_digital_silence'] for value in self.measurements)
        self.upload_items=tuple(item for item,sent in zip(attachments,self.sent) if sent)

    @property
    def all_skipped(self):
        return bool(self.images) and not self.upload_items

    def supports(self,registry,model):
        caps=registry.entries.get(str(model).lower()) or registry.entries.get(registry.canonical(str(model))) or {}
        audio=caps.get('audio_input')
        if not (registry.source=='gateway' and caps.get('route')=='antigravity' and caps.get('family')=='gemini'
                and isinstance(audio,dict) and type(audio.get('version')) is int and audio['version']==1
                and audio.get('transport_supported') is True and audio.get('format')=='pcm_wav'
                and audio.get('content_type')==PART_TYPE and audio.get('streaming') is False
                and audio.get('backend_acceptance')=='unverified' and audio.get('requires_probe_opt_in') is True):
            return False
        selected=self.upload_items or self.images
        requirements={'max_files':len(selected),'max_file_bytes':max(item.size for item in selected),
                      'max_total_bytes':sum(item.size for item in selected),
                      'max_duration_seconds':max((item.descriptor['duration_ms']+999)//1000 for item in selected)}
        return all(type(audio.get(key)) is int and audio[key]>=required for key,required in requirements.items())

    def require(self,registry,model,stage):
        require(self.probe,'Audio requires --probe-unverified-audio to authorize an upload to this unverified backend')
        require(self.supports(registry,model),'Audio requires an advertised experimental Gemini PCM-WAV route; text-only fallback is refused')
        if self.single_backend_attempt:
            caps=registry.entries.get(str(model).lower()) or registry.entries.get(registry.canonical(str(model))) or {}
            limit=caps.get('audio_input',{}).get('backend_attempt_limit')
            require(type(limit) is int and limit==1,'listen requires a gateway advertising a single backend attempt for audio')
        with self.lock:
            if len(self.checked)<128:self.checked.add((str(model),str(stage)))

    def input(self,prompt):
        require(self.probe,'Explicit unverified-audio probe upload intent is required')
        content = [{'type':'input_text','text':prompt}]
        for index, (item, sent) in enumerate(zip(self.images, self.sent)):
            label = f'Audio clip {chr(65 + index)} (attachment {index + 1} of {len(self.images)})'
            if sent:
                content.extend([{'type':'input_text','text':label + ':'}, item.part()])
            else:
                content.append({'type':'input_text','text':label + ' was not sent: Anti measured every sample as zero (exact digital silence).'})
        return [{'role':'user','content':content}]

    def verify_payload(self,payload):
        require(parts(payload)==[item.part() for item in self.upload_items],'Audio bytes/order or probe intent changed before submission')

    def report(self):
        value=super().report();audio=value.pop('images')
        value.pop('pixel_secret_scan');value.pop('provider_image_acceptance')
        value.update(kind='audio',audio=audio,format_validation='classic_pcm_wav',audio_secret_scan='not_performed',
            captured_duration_ms=sum(item['duration_ms'] for item in audio),probe_upload_intent=self.probe,
            provider_audio_acceptance='unverified',listening_verification='not_run')
        labels=[chr(65+index) for index in range(len(self.images))]
        value.update(submitted_count=sum(self.sent),measured_silent_skipped_count=self.sent.count(False))
        measurement={'schemaVersion':1,'basis':MEASUREMENT_BASIS,'digital_silence_policy':self.silence_policy,
            'clips':[{'index':index+1,'label':labels[index],'sha256':item.descriptor['sha256'],
                      **json.loads(json.dumps(measured)),
                      'measured_result':'exact_digital_silence' if measured['wholly_digital_silence'] else None,
                      'submitted_to_model':sent}
                     for index,(item,measured,sent) in enumerate(zip(self.images,self.measurements,self.sent))]}
        observation={'basis':'provider_model_output','status':'requested' if any(self.sent) else 'not_requested',
            'clips':[label for label,sent in zip(labels,self.sent) if sent],
            'provider_audio_acceptance':'unverified','listening_verification':'not_run'}
        value.update(measurement=measurement,model_observation=observation)
        return value


CLIP_FIELDS={'index','label','sha256','window_ms','quiet_peak_dbfs','wholly_digital_silence','peak_dbfs','rms_dbfs',
             'windows','segments','segments_omitted','measured_result','submitted_to_model'}
OBSERVATION_FIELDS={'basis','status','clips','provider_audio_acceptance','listening_verification'}


def _level(value):
    return value is None or (type(value) in (int,float) and math.isfinite(value) and -200<=value<=0)


def _segments_valid(segments,duration_ms):
    return isinstance(segments,list) and len(segments)<=MAX_SEGMENTS and all(
        isinstance(row,list) and len(row)==3 and type(row[0]) is int and type(row[1]) is int
        and 0<=row[0]<=row[1]<=duration_ms and row[2] in WINDOW_STATES for row in segments)


def _clip_valid(clip,index,item):
    windows=clip.get('windows') if isinstance(clip,dict) else None
    return (isinstance(clip,dict) and set(clip)==CLIP_FIELDS and type(clip['index']) is int and clip['index']==index
            and clip['label']==chr(64+index) and clip['sha256']==item['sha256'] and clip['window_ms']==WINDOW_MS
            and clip['quiet_peak_dbfs']==QUIET_PEAK_DBFS and type(clip['wholly_digital_silence']) is bool
            and type(clip['submitted_to_model']) is bool and _level(clip['peak_dbfs']) and _level(clip['rms_dbfs'])
            and clip['measured_result']==('exact_digital_silence' if clip['wholly_digital_silence'] else None)
            and isinstance(windows,dict) and set(windows)==set(WINDOW_STATES)
            and all(type(count) is int and count>=0 for count in windows.values())
            and type(clip['segments_omitted']) is int and clip['segments_omitted']>=0
            and _segments_valid(clip['segments'],item['duration_ms']))


def _measurement_receipt(measurement,observation,items):
    if (not isinstance(measurement,dict) or set(measurement)!={'schemaVersion','basis','digital_silence_policy','clips'}
            or measurement['schemaVersion']!=1 or measurement['basis']!=MEASUREMENT_BASIS
            or measurement['digital_silence_policy'] not in SILENCE_POLICIES
            or not isinstance(measurement['clips'],list) or len(measurement['clips'])!=len(items)
            or not all(_clip_valid(clip,index,item) for index,(clip,item) in enumerate(zip(measurement['clips'],items),1))):
        return None
    labels=[clip['label'] for clip in measurement['clips'] if clip['submitted_to_model']]
    if (not isinstance(observation,dict) or set(observation)!=OBSERVATION_FIELDS
            or observation['basis']!='provider_model_output' or observation['clips']!=labels
            or observation['status']!=('requested' if labels else 'not_requested')
            or observation['provider_audio_acceptance']!='unverified' or observation['listening_verification']!='not_run'):
        return None
    return {'measurement':json.loads(json.dumps(measurement)),'model_observation':{**observation,'clips':list(labels)}}


def projection(value,*,hashes=False):
    if not isinstance(value,dict) or value.get('kind')!='audio' or type(value.get('schemaVersion')) is not int or value['schemaVersion']!=1:return None
    for key,limit in (('captured_count',MAX_FILES),('captured_bytes',MAX_TOTAL_BYTES),('captured_duration_ms',60000),('gateway_attempts',2**63-1)):
        if type(value.get(key)) is not int or not 0<=value[key]<=limit:return None
    if type(value.get('probe_upload_intent')) is not bool:return None
    result={key:value[key] for key in ('schemaVersion','kind','captured_count','captured_bytes','captured_duration_ms','gateway_attempts','probe_upload_intent')}
    result.update(status='attempted' if value['gateway_attempts'] else 'not_sent',coverage_basis='captured_bytes_in_each_attempt',
        format_validation='classic_pcm_wav',audio_secret_scan='not_performed',provider_audio_acceptance='unverified',
        listening_verification='not_run',real_media_evaluation='not_run',trace_retained=False,audio_hashes_retained=False)
    for key in ('submitted_count','measured_silent_skipped_count'):
        if key in value:
            if type(value[key]) is not int or not 0<=value[key]<=value['captured_count']:return None
            result[key]=value[key]
    if hashes:
        items=value.get('audio')
        if (not valid_identity(items) or len(items)!=value['captured_count'] or sum(item['bytes'] for item in items)!=value['captured_bytes']
                or sum(item['duration_ms'] for item in items)!=value['captured_duration_ms']):return None
        result.update(audio=[dict(item) for item in items],audio_hashes_retained=True)
        if 'measurement' in value or 'model_observation' in value:
            receipt=_measurement_receipt(value.get('measurement'),value.get('model_observation'),items)
            if receipt is None:return None
            result.update(receipt)
    return result
