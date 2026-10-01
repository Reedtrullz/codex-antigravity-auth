"""Explicit bounded image capture and count-only media coverage; no decoder/network."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import threading

from .errors import AntiError
from .inventory import read_file
from .data_policy import content_digest

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
_FALLBACK = ContextVar('anti_media_fallback', default=False)


class MediaError(AntiError):
    pass


@dataclass(frozen=True)
class Image:
    index: int
    mime: str
    size: int
    sha256: str
    data_url: str = field(repr=False)

    def identity(self):
        return {'index':self.index,'mime':self.mime,'bytes':self.size,'sha256':self.sha256}


def capture(paths, *, policy=None):
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_IMAGES:
        raise MediaError(f'Use one to {MAX_IMAGES} explicit local images')
    selected = []
    for index, value in enumerate(paths, 1):
        if (not isinstance(value,str) or not value or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://',value)
                or value.startswith('data:') or any(ord(char)<32 or 127<=ord(char)<160 for char in value)):
            raise MediaError(f'Image {index} must be an explicit local file, not a URL or inline payload')
        try:
            value.encode('utf-8')
            path = Path(value).expanduser().absolute()
            if path.is_symlink():raise MediaError(f'Image {index} must not be a symlink')
            selected.append(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise MediaError(f'Image {index} has an invalid local path') from exc
    if policy is not None:
        policy.check_paths(selected,root=policy.root)
    images = []
    remaining = MAX_TOTAL_BYTES
    for index, path in enumerate(selected, 1):
        try:
            # Preserve every selected component for the no-follow reader. Resolving
            # first would erase a symlinked parent before its descriptor checks.
            # Policy already checked logical/resolved containment above; anchoring
            # at the filesystem root also checks parents above the policy root.
            root = Path(path.anchor)
            relative = path.relative_to(root).as_posix()
            raw, _size, reason = read_file(root,relative,min(MAX_IMAGE_BYTES,remaining))
        except (OSError, ValueError) as exc:
            raise MediaError(f'Image {index} could not be captured as an allowed regular file') from exc
        if reason or raw is None:
            raise MediaError(f'Image {index} capture refused ({reason}); limits are 2 MiB each and 4 MiB total')
        if raw.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif raw.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        else:
            raise MediaError(f'Image {index} is not a supported PNG/JPEG signature; audio/video and text fallbacks are refused')
        remaining -= len(raw)
        images.append(Image(index,mime,len(raw),hashlib.sha256(raw).hexdigest(),
                            'data:'+mime+';base64,'+base64.b64encode(raw).decode('ascii')))
    return Session(tuple(images))


def supports(registry, model):
    caps = registry.entries.get(str(model).lower()) or registry.entries.get(registry.canonical(str(model))) or {}
    effective = caps.get('effective',{})
    forms = effective.get('image_forms')
    return (registry.source == 'gateway' and 'image' in effective.get('input_modalities',[])
            and isinstance(forms,list) and 'data_url' in forms)


@contextmanager
def submission_context(fallback=False):
    token = _FALLBACK.set(bool(fallback))
    try:yield
    finally:_FALLBACK.reset(token)


class Session:
    def __init__(self, images):
        self.images = images
        self.lock = threading.Lock()
        self.attempts = 0
        self.trace = []
        self.checked = set()

    def identity(self):
        return [image.identity() for image in self.images]

    def content_sha256(self, prompt):
        return content_digest(prompt,self.identity())

    def require(self, registry, model, stage):
        if not supports(registry,model):
            raise MediaError(f'Image attachments require a gateway declaration of data-URL image support for the {stage} stage; no text-only substitution')
        with self.lock:
            if len(self.checked)<128:self.checked.add((str(model),str(stage)))

    def input(self, prompt):
        return [{'role':'user','content':[{'type':'input_text','text':prompt},
            *[{'type':'input_image','image_url':image.data_url} for image in self.images]]}]

    def verify_payload(self, payload):
        value = payload.get('input') if isinstance(payload,dict) else None
        found = []
        if isinstance(value,list):
            for item in value:
                if isinstance(item,dict) and isinstance(item.get('content'),list):
                    found.extend(part.get('image_url') for part in item['content']
                                 if isinstance(part,dict) and part.get('type')=='input_image')
        if found != [image.data_url for image in self.images]:
            raise MediaError('Attachment bytes changed or were omitted before submission; refusing text-only fallback')

    def submitted(self, model, stage):
        with self.lock:
            self.attempts += 1
            if len(self.trace)<128:
                self.trace.append({'model':str(model),'stage':str(stage),'fallback':_FALLBACK.get()})

    def report(self):
        with self.lock:
            return {'schemaVersion':1,'captured_count':len(self.images),'captured_bytes':sum(image.size for image in self.images),
                'images':self.identity(),'gateway_attempts':self.attempts,
                'status':'attempted' if self.attempts else 'not_sent','coverage_basis':'captured_bytes_in_each_attempt',
                'pixel_secret_scan':'not_performed','format_validation':'signature_only',
                'real_media_evaluation':'not_run','provider_image_acceptance':'unverified',
                'checked_routes':[{'model':model,'stage':stage} for model,stage in sorted(self.checked)],
                'attempts':list(self.trace),'attempts_omitted':max(0,self.attempts-len(self.trace))}


def projection(value, *, hashes=False):
    """Bounded persistence receipts; never-mode omits image content hashes."""
    if not isinstance(value,dict) or type(value.get('schemaVersion')) is not int or value['schemaVersion']!=1:
        return None
    for key, limit in (('captured_count',MAX_IMAGES),('captured_bytes',MAX_TOTAL_BYTES),('gateway_attempts',2**63-1)):
        if type(value.get(key)) is not int or not 0 <= value[key] <= limit:return None
    result={'schemaVersion':1,**{key:value[key] for key in ('captured_count','captured_bytes','gateway_attempts')},
            'status':'attempted' if value['gateway_attempts'] else 'not_sent',
            'coverage_basis':'captured_bytes_in_each_attempt','pixel_secret_scan':'not_performed',
            'format_validation':'signature_only','real_media_evaluation':'not_run',
            'provider_image_acceptance':'unverified','trace_retained':False,'image_hashes_retained':False}
    if hashes:
        images=value.get('images')
        if not isinstance(images,list) or len(images)!=value['captured_count']:return None
        for index,image in enumerate(images,1):
            if (not isinstance(image,dict) or set(image)!={'index','mime','bytes','sha256'} or type(image['index']) is not int
                    or image['index']!=index or not isinstance(image['mime'],str) or image['mime'] not in {'image/png','image/jpeg'}
                    or type(image['bytes']) is not int or not 0<image['bytes']<=MAX_IMAGE_BYTES
                    or not isinstance(image['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',image['sha256'])):return None
        if sum(image['bytes'] for image in images)!=value['captured_bytes']:return None
        result.update(images=[dict(image) for image in images],image_hashes_retained=True)
    return result
