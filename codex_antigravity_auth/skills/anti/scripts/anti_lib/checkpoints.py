"""Explicit full-retention checkpoints under the existing per-run writer lock."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

from .artifacts import validate_record, RECORD_SCHEMA_VERSION, ArtifactError
from .cleanup import RUN_ID_RE, TERMINAL_STATES, assert_not_deleted
from .errors import AntiError
from .inventory import read_path
from .persistence import atomic_write_json, file_lock, fsync_directory
from .redaction import sanitize_json
from .retention import control_metadata

VERSION = 1
MAX_CHUNKS = 512
MAX_EVENTS = 2048
MAX_LINEAGE_RUNS = 64
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
REF = re.compile(r'checkpoints/(?:events|manifests)/[0-9a-f]{32}\.json\Z')
STATUSES = {'success', 'failed', 'not_sent', 'truncated', 'non_answer', 'incomplete', 'error'}


class CheckpointError(AntiError):
    pass


def encoded(value):
    try:
        return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise CheckpointError('Checkpoint contains unsupported structured data') from exc


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise CheckpointError(message)


def read_json(path):
    raw, _size, reason = read_path(path, max_file_bytes=MAX_FILE_BYTES)
    if reason == 'symlink':
        require(False, 'Checkpoint path is a symlink; preserve it for inspection')
    if reason == 'file_byte_limit':
        require(False, 'Checkpoint exceeds its file limit')
    if reason is not None:
        raise CheckpointError('Checkpoint is missing or invalid; preserve the original run')
    try:
        return json.loads(raw), raw
    except (OSError, ValueError, RecursionError) as exc:
        raise CheckpointError('Checkpoint is missing or invalid; preserve the original run') from exc


def safe_run(root, run_id):
    require(isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id), 'Invalid checkpoint run id')
    require(not root.is_symlink(), 'Checkpoint root must not be a symlink')
    assert_not_deleted(root, run_id)
    directory = root / run_id
    require(not directory.is_symlink(), 'Checkpoint artifact directory must not be a symlink')
    return directory


def read_ref(directory, ref):
    require(isinstance(ref, dict) and set(ref) == {'path', 'sha256', 'bytes'}, 'Invalid checkpoint reference')
    require(isinstance(ref['path'], str) and REF.fullmatch(ref['path']), 'Invalid checkpoint reference path')
    require(isinstance(ref['sha256'], str) and SHA.fullmatch(ref['sha256']), 'Invalid checkpoint digest')
    require(type(ref['bytes']) is int and 0 < ref['bytes'] <= MAX_FILE_BYTES, 'Invalid checkpoint size')
    path = directory / ref['path']
    require(not path.parent.is_symlink() and not path.parent.parent.is_symlink(), 'Unsafe checkpoint parent')
    value, raw = read_json(path)
    require(len(raw) == ref['bytes'] and hashlib.sha256(raw).hexdigest() == ref['sha256'], 'Checkpoint bytes changed; reuse refused')
    return value


def write_new(directory, category, value):
    raw = encoded(value)
    require(len(raw) <= MAX_FILE_BYTES, 'Checkpoint entry exceeds its retention limit')
    parent = directory / 'checkpoints' / category
    for item in (directory, directory / 'checkpoints', parent):
        require(not item.is_symlink(), 'Unsafe checkpoint destination')
        item.mkdir(mode=0o700, exist_ok=True)
        require(item.is_dir(), 'Checkpoint destination must be a directory')
    path = parent / (uuid.uuid4().hex + '.json')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags, 0o600), 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(parent)
    return {'path': path.relative_to(directory).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def generation_projection(value):
    """Only outcome/identity/counter evidence is needed to reuse a chunk."""
    result = {}
    if not isinstance(value, dict):return result
    for key in ('actual_model','actual_provider','requested_model','model_used','backend_model','fallback_model','fallback_policy'):
        item = value.get(key)
        if isinstance(item,str) and len(item) <= 512:result[key] = item
    for key in ('fallback_used','submitted','upstream_output_empty'):
        if type(value.get(key)) is bool:result[key] = value[key]
    for key in ('upstream_status','terminal_kind','terminal_reason'):
        if isinstance(value.get(key),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value[key]):result[key] = value[key]
    if isinstance(value.get('gateway_routing_identity'),str) and SHA.fullmatch(value['gateway_routing_identity']):
        result['gateway_routing_identity'] = value['gateway_routing_identity']
    usage = value.get('usage')
    if isinstance(usage,dict):
        result['usage'] = {key:item for key,item in usage.items() if key in {'input_tokens','output_tokens','total_tokens'}
                           and type(item) is int and 0 <= item <= 2**63-1}
    if type(value.get('elapsed_ms')) is int and 0 <= value['elapsed_ms'] <= 2**63-1:
        result['elapsed_ms'] = value['elapsed_ms']
    return result


class Checkpoint:
    def __init__(self, root, run_id, writer_id, *, recipe, chunks, routes, resume=None, rerun=()):
        self.root, self.run_id, self.writer_id = Path(root), run_id, writer_id
        require(isinstance(recipe, str) and SHA.fullmatch(recipe), 'Checkpoint recipe identity is unavailable')
        require(isinstance(chunks, list) and 0 < len(chunks) <= MAX_CHUNKS
                and all(isinstance(value, str) and SHA.fullmatch(value) for value in chunks), 'Invalid or oversized checkpoint chunk plan')
        require(isinstance(routes, (list, tuple, set)) and routes and all(isinstance(value, str) and SHA.fullmatch(value) for value in routes), 'Checkpoint needs declared route identities')
        self.recipe, self.chunks, self.routes = recipe, chunks, set(routes)
        self.events, self.refs, self.latest = [], [], {}
        self.prior_runs = {}
        self.source = resume
        self.reference = None
        self.reused = set()
        self.rerun = set(rerun)
        require(all(type(value) is int and 1 <= value <= len(chunks) for value in self.rerun), 'Rerun chunk selection is outside the immutable plan')
        require(resume is not None or not self.rerun, 'Rerun chunk selection requires a resume source')
        if resume is not None:
            require(resume != run_id, 'Resume must use a new run id; the original run is immutable')
            self._load_source(resume)
        self.publish()

    def _record(self, run_id):
        safe_run(self.root, run_id)
        path = self.root / (run_id + '.json')
        value, _ = read_json(path)
        require(isinstance(value, dict) and type(value.get('recordSchemaVersion')) is int
                and value['recordSchemaVersion'] == RECORD_SCHEMA_VERSION, 'Checkpoint requires a current owned run record')
        try:
            validate_record(value, path)
        except ArtifactError as exc:
            raise CheckpointError('Checkpoint source or writer publication is invalid; preserve the saved run') from exc
        require(value.get('id') == run_id and value.get('save_output') == 'full', 'Checkpoint requires a full-retention run')
        require(isinstance(value.get('metadata'), dict), 'Checkpoint record metadata must be an object')
        return value

    def _load_source(self, run_id):
        safe_run(self.root, run_id)
        with file_lock(self.root / (run_id + '.json')):
            record = self._record(run_id)
            require(record.get('status') in TERMINAL_STATES, 'Resume source must be terminal; a running or uncertain run cannot be reused')
            receipt = record.get('metadata', {}).get('checkpoint')
            require(isinstance(receipt, dict) and type(receipt.get('schemaVersion')) is int and receipt['schemaVersion'] == VERSION, 'Run has no compatible explicit checkpoint')
            manifest = read_ref(self.root / run_id, receipt.get('manifest'))
            require(isinstance(manifest, dict) and type(manifest.get('schemaVersion')) is int
                    and manifest['schemaVersion'] == VERSION and manifest.get('runId') == run_id, 'Invalid checkpoint manifest')
            require(manifest.get('recipe') == self.recipe and manifest.get('chunks') == self.chunks
                    and manifest.get('routes') == sorted(self.routes), 'Source, prompt, policy, catalog, helper or route changed; reuse refused')
            refs = manifest.get('events')
            require(isinstance(refs, list) and len(refs) <= MAX_EVENTS, 'Invalid checkpoint event collection')
            total_bytes = 0
            for ref in refs:
                entry = read_ref(self.root / run_id, ref)
                total_bytes += ref['bytes']
                require(total_bytes <= MAX_TOTAL_BYTES, 'Checkpoint history exceeds its retention limit')
                self._validate_event(entry)
                require(not any(old['eventId'] == entry['eventId'] for old in self.events), 'Duplicate checkpoint event identity')
                self.events.append(entry)
                self.latest[entry['index']] = entry
            prior = manifest.get('priorRuns', {})
            require(isinstance(prior, dict) and len(prior) + 2 <= MAX_LINEAGE_RUNS and all(RUN_ID_RE.fullmatch(key) for key in prior), 'Invalid checkpoint run lineage')
            require(run_id not in prior and self.run_id not in prior, 'Checkpoint lineage must not contain cycles')
            require(all(isinstance(value, dict) and self.accounting(value) == value for value in prior.values()), 'Invalid checkpoint accounting')
            self.prior_runs = dict(prior)
            self.prior_runs[run_id] = self.accounting(record.get('metadata', {}))
        for index, entry in self.latest.items():
            if index in self.rerun or entry['status'] == 'not_sent':
                continue
            require(entry['status'] == 'success' and entry['reusable'],
                    f'Chunk {index} is incomplete or not reusable; select --rerun-chunk {index} explicitly')
            self.reused.add(index)

    def _validate_event(self, entry):
        require(isinstance(entry, dict) and set(entry) == {'eventId','index','promptSha256','status','output','model','generation','route','reusable'},
                'Invalid checkpoint event')
        index = entry['index']
        require(type(index) is int and 1 <= index <= len(self.chunks) and entry['promptSha256'] == self.chunks[index-1], 'Checkpoint chunk identity changed')
        require(isinstance(entry['eventId'], str) and re.fullmatch(r'[0-9a-f]{32}', entry['eventId']), 'Invalid checkpoint event identity')
        require(isinstance(entry['status'], str) and entry['status'] in STATUSES and type(entry['reusable']) is bool, 'Invalid checkpoint outcome')
        require(isinstance(entry['output'], str) and isinstance(entry['model'], str) and isinstance(entry['generation'], dict), 'Invalid checkpoint payload')
        require(generation_projection(entry['generation']) == entry['generation'], 'Invalid checkpoint generation evidence')
        require(entry['route'] is None or (isinstance(entry['route'], str) and entry['route'] in self.routes), 'Checkpoint actual route changed')
        if entry['reusable']:
            require(entry['status'] == 'success' and bool(entry['output'].strip()) and entry['route'] in self.routes,
                    'Checkpoint does not contain a completed routed output')

    @staticmethod
    def accounting(metadata):
        # These are evidence snapshots with their original per-run scopes. Do not
        # reconstruct active allowances or equate token counters with billing.
        result = control_metadata(metadata)
        value = metadata.get('budget_committed')
        if metadata.get('cost_units') == 'heuristic_units' and type(value) in (int, float) and 0 <= value <= 2**63-1 and math.isfinite(value):
            result.update(budget_committed=value, cost_units='heuristic_units')
        return result

    def take(self, index):
        return self.latest[index] if index in self.reused else None

    def record(self, index, *, status, output='', model='', generation=None, route=None):
        require(type(index) is int and 1 <= index <= len(self.chunks), 'Invalid checkpoint chunk index')
        require(len(self.events) < MAX_EVENTS, 'Checkpoint attempt history is full; start a new run')
        value = {'eventId':uuid.uuid4().hex,'index':index,'promptSha256':self.chunks[index-1],
                 'status':status if status in STATUSES else 'incomplete','output':output,'model':model,
                 'generation':generation_projection(generation),'route':route if route in self.routes else None,'reusable':False}
        clean = sanitize_json(value)
        clean['reusable'] = status == 'success' and bool(output.strip()) and clean == value and route in self.routes
        self._validate_event(clean)
        require(sum(len(encoded(row)) for row in self.events) + len(encoded(clean)) <= MAX_TOTAL_BYTES, 'Checkpoint history exceeds its retention limit')
        self.events.append(clean)
        self.latest[index] = clean
        self.reused.discard(index)
        self.publish()

    def publish(self):
        directory = safe_run(self.root, self.run_id)
        with file_lock(self.root / (self.run_id + '.json')):
            record = self._record(self.run_id)
            require(record.get('writerId') == self.writer_id and record.get('status') == 'running', 'Checkpoint writer no longer owns a running record')
            require(self.reference is not None or not record.get('metadata', {}).get('checkpoint'),
                    'Run already has a checkpoint; initialize a new run instead of replacing it')
            while len(self.refs) < len(self.events):
                self.refs.append(write_new(directory, 'events', self.events[len(self.refs)]))
            manifest = {'schemaVersion':VERSION,'runId':self.run_id,'recipe':self.recipe,'chunks':self.chunks,
                        'routes':sorted(self.routes),'events':list(self.refs),'priorRuns':self.prior_runs}
            ref = write_new(directory, 'manifests', manifest)
            receipt = {'schemaVersion':VERSION,'manifest':ref,'sourceRun':self.source,
                       'reusedChunks':sorted(self.reused),'priorRuns':self.prior_runs,
                       'chunks':[{'index':index,'status':self.latest.get(index,{}).get('status','not_sent'),
                                  'reusable':self.latest.get(index,{}).get('reusable',False)}
                                 for index in range(1,len(self.chunks)+1)]}
            record.setdefault('metadata', {})['checkpoint'] = receipt
            validate_record(record, self.root / (self.run_id + '.json'))
            try:
                atomic_write_json(self.root / (self.run_id + '.json'), record)
            except OSError:
                # A directory-fsync failure can follow a successful replace.
                # Keep the in-memory pointer aligned without claiming success.
                try:
                    current, _ = read_json(self.root / (self.run_id + '.json'))
                    if current.get('metadata', {}).get('checkpoint') == receipt:
                        self.reference = receipt
                except (CheckpointError, AttributeError):
                    pass
                raise
            self.reference = receipt
