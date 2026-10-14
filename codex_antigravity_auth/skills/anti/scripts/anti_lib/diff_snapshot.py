"""Immutable coordinate indexing for ordinary Git unified patches; no filesystem reads."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import re

MAX_CHARS = 4 * 1024 * 1024
MAX_FILES = 5000
MAX_HUNKS = 5000
MAX_ROWS = 50_000
MAX_LINE_CHARS = 16_384
MAX_COORDINATE = 2**31 - 1
_HUNK = re.compile(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\Z')
_INDEX = re.compile(r'index ([0-9a-f]+)\.\.([0-9a-f]+)(?: ([0-7]{6}))?\Z')


class UnsupportedDiff(ValueError):
    pass


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _path(raw, prefix=''):
    if raw == '/dev/null': return None
    if raw.startswith('"'):
        if not raw.endswith('"'): raise UnsupportedDiff('malformed_path')
        encoded = bytearray()
        cursor = 1
        escapes = {'a':7, 'b':8, 't':9, 'n':10, 'v':11, 'f':12, 'r':13, '"':34, '\\':92}
        while cursor < len(raw) - 1:
            char = raw[cursor]
            cursor += 1
            if char != '\\':
                encoded.extend(char.encode('utf-8'))
                continue
            if cursor >= len(raw) - 1: raise UnsupportedDiff('malformed_path')
            char = raw[cursor]
            if char in escapes:
                encoded.append(escapes[char]); cursor += 1
            elif re.fullmatch(r'[0-3][0-7]{2}', raw[cursor:cursor+3]):
                encoded.append(int(raw[cursor:cursor+3], 8)); cursor += 3
            else:
                raise UnsupportedDiff('malformed_path')
        try: raw = encoded.decode('utf-8')
        except UnicodeError: raise UnsupportedDiff('non_utf8_path') from None
    if prefix:
        if not raw.startswith(prefix): raise UnsupportedDiff('unsupported_path_prefix')
        raw = raw[len(prefix):]
    if not raw or '\0' in raw or raw.startswith('/') or '..' in PurePosixPath(raw).parts:
        raise UnsupportedDiff('unsafe_path')
    return raw


def _blob(value):
    if len(value) not in {40, 64}: raise UnsupportedDiff('abbreviated_blob_identity')
    return value if value.strip('0') else None


def _file(lines, allowed, budget):
    result = {'oldPath':None, 'newPath':None, 'oldBlob':None, 'newBlob':None,
              'oldMode':None, 'newMode':None, 'hunks':[], 'status':'unknown', 'reason':None}
    rows = []
    rename = {}
    seen = set()
    cursor = 1
    try:
        while cursor < len(lines):
            raw, start, end = lines[cursor]
            line = raw.removesuffix('\n')
            if len(line) > MAX_LINE_CHARS: raise UnsupportedDiff('diff_line_limit')
            if line.startswith(('--- ', '+++ ')):
                side = 'old' if line.startswith('--- ') else 'new'
                if side in seen: raise UnsupportedDiff('duplicate_file_header')
                seen.add(side)
                result[side+'Path'] = _path(line[4:].removesuffix('\t'), 'a/' if side == 'old' else 'b/')
            elif line.startswith(('rename from ', 'copy from ')):
                rename['oldPath'] = _path(line.split(' from ', 1)[1])
            elif line.startswith(('rename to ', 'copy to ')):
                rename['newPath'] = _path(line.split(' to ', 1)[1])
            elif line.startswith('index '):
                if 'index' in seen: raise UnsupportedDiff('duplicate_index_header')
                seen.add('index')
                match = _INDEX.fullmatch(line)
                if not match: raise UnsupportedDiff('unsupported_index_header')
                result['oldBlob'], result['newBlob'] = _blob(match[1]), _blob(match[2])
                if match[3]: result['oldMode'] = result['newMode'] = match[3]
            elif line.startswith(('old mode ', 'new mode ', 'deleted file mode ', 'new file mode ')):
                mode = line.rsplit(' ', 1)[-1]
                if not re.fullmatch('[0-7]{6}', mode): raise UnsupportedDiff('unsupported_mode')
                result['oldMode' if line.startswith(('old ', 'deleted ')) else 'newMode'] = mode
            elif line.startswith(('Binary files ', 'GIT binary patch')):
                raise UnsupportedDiff('binary_diff')
            elif line.startswith('@@'):
                match = _HUNK.fullmatch(line)
                if not match: raise UnsupportedDiff('unsupported_hunk_header')
                if any(len(value) > 10 for value in match.groups() if value is not None):
                    raise UnsupportedDiff('coordinate_limit')
                old, old_count, new, new_count = (int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))
                if max(old+old_count, new+new_count) > MAX_COORDINATE or (old_count and not old) or (new_count and not new):
                    raise UnsupportedDiff('coordinate_limit')
                budget[0] -= 1
                if budget[0] < 0 or old_count+new_count > budget[1] * 2:
                    raise UnsupportedDiff('diff_coordinate_limit')
                hunk = {'oldStart':old, 'oldCount':old_count, 'newStart':new, 'newCount':new_count,
                        'patchStart':start, 'patchEnd':end, 'id':f'hunk-{start}'}
                used_old = used_new = 0
                cursor += 1
                while cursor < len(lines):
                    raw, row_start, row_end = lines[cursor]
                    if used_old == old_count and used_new == new_count:
                        if raw == '\\ No newline at end of file\n':
                            if not rows or rows[-1]['hunkId'] != hunk['id'] or not rows[-1]['newline']:
                                raise UnsupportedDiff('malformed_newline_marker')
                            rows[-1]['newline'] = False
                            rows[-1]['patchEnd'] = row_end
                            hunk['patchEnd'] = row_end
                            cursor += 1
                        break
                    if raw == '\\ No newline at end of file\n':
                        if not rows or rows[-1]['hunkId'] != hunk['id'] or not rows[-1]['newline']:
                            raise UnsupportedDiff('malformed_newline_marker')
                        rows[-1]['newline'] = False
                        rows[-1]['patchEnd'] = row_end
                        cursor += 1
                        continue
                    if not raw or raw[0] not in ' +-': raise UnsupportedDiff('malformed_hunk')
                    kind = raw[0]
                    text = raw[1:].removesuffix('\n')
                    if len(text) > MAX_LINE_CHARS: raise UnsupportedDiff('diff_line_limit')
                    budget[1] -= 1
                    if budget[1] < 0: raise UnsupportedDiff('diff_coordinate_limit')
                    row = {'oldLine':old+used_old if kind in ' -' else None,
                           'newLine':new+used_new if kind in ' +' else None,
                           'kind':{' ':'context', '+':'added', '-':'deleted'}[kind],
                           'text':text, 'excerptSha256':_hash(text), 'newline':True,
                           'patchStart':row_start, 'patchEnd':row_end, 'hunkId':hunk['id']}
                    used_old += kind in ' -'
                    used_new += kind in ' +'
                    if used_old > old_count or used_new > new_count: raise UnsupportedDiff('malformed_hunk')
                    rows.append(row)
                    hunk['patchEnd'] = row_end
                    cursor += 1
                if used_old != old_count or used_new != new_count: raise UnsupportedDiff('incomplete_hunk')
                result['hunks'].append(hunk)
                continue
            elif line and not line.startswith(('similarity index ', 'dissimilarity index ')):
                raise UnsupportedDiff('unsupported_patch_line')
            cursor += 1
        for side in ('old', 'new'):
            key = side + 'Path'
            if key in rename:
                if result[key] is not None and result[key] != rename[key]: raise UnsupportedDiff('conflicting_rename')
                result[key] = rename[key]
            if result[key] is not None and result[key] not in allowed: raise UnsupportedDiff('path_outside_selected_diff')
            if result[side+'Mode'] not in {None, '100644', '100755'}: raise UnsupportedDiff('nonregular_diff')
        if not result['hunks']: raise UnsupportedDiff('no_text_hunks')
        if result['oldPath'] is None and result['newPath'] is None: raise UnsupportedDiff('missing_file_headers')
        # A present side must have a captured full blob identity. Zero IDs are
        # explicit unknowns and never treated as proof of current disk content.
        for side in ('old', 'new'):
            if result[side+'Path'] is not None and result[side+'Blob'] is None:
                raise UnsupportedDiff('missing_blob_identity')
        result['status'] = 'captured'
    except UnsupportedDiff as exc:
        result['reason'] = str(exc)
        result['hunks'] = []
        rows = []
    result['_rows'] = rows
    return result


def capture(diff, paths):
    snapshot = {'version':1, 'status':'complete', 'diffSha256':None, 'files':[], '_lookup':{}}
    if len(diff) > MAX_CHARS:
        return {**snapshot, 'status':'unavailable', 'reason':'diff_coordinate_limit'}
    if diff.count('\n') > MAX_ROWS * 2 + MAX_HUNKS + MAX_FILES * 10:
        return {**snapshot, 'status':'unavailable', 'reason':'diff_coordinate_limit'}
    snapshot['diffSha256'] = _hash(diff)
    sections = []
    offset = 0
    # Git patch lines use LF; Unicode separators inside source are ordinary text.
    for part in diff.split('\n')[:-1]:
        raw = part + '\n'
        if raw.startswith(('diff --git ', 'diff --cc ', 'diff --combined ')):
            if len(sections) >= MAX_FILES:
                return {**snapshot, 'status':'unavailable', 'reason':'diff_coordinate_limit'}
            sections.append([])
        if sections: sections[-1].append((raw, offset, offset+len(raw)))
        offset += len(raw)
    if diff and not diff.endswith('\n'):
        return {**snapshot, 'status':'unavailable', 'reason':'incomplete_patch'}
    if len(sections) > MAX_FILES:
        return {**snapshot, 'status':'unavailable', 'reason':'diff_coordinate_limit'}
    budget = [MAX_HUNKS, MAX_ROWS]
    allowed = set(paths)
    for lines in sections:
        if not lines[0][0].startswith('diff --git '):
            snapshot['status'] = 'partial'
            snapshot['files'].append({'status':'unknown', 'reason':'combined_diff', 'hunks':[]})
            continue
        file = _file(lines, allowed, budget)
        snapshot['files'].append(file)
        if file['status'] != 'captured': snapshot['status'] = 'partial'
        for row in file['_rows']:
            for side in ('old', 'new'):
                line = row[side+'Line']
                if line is not None and file[side+'Path'] is not None:
                    key = (file[side+'Path'], side, line)
                    snapshot['_lookup'].setdefault(key, []).append((file, row))
    if diff.strip() and not sections:
        snapshot.update(status='unavailable', reason='unsupported_patch')
    return snapshot


def summary(snapshot):
    return {key:([{k:v for k,v in file.items() if not k.startswith('_')} for file in value]
                 if key == 'files' else value) for key,value in snapshot.items() if not key.startswith('_')}


def locate(snapshot, path, line, side, ranges):
    if not isinstance(side, str) or side not in {'old', 'new'}: return None, 'unsupported_side'
    if not isinstance(path, str): return None, 'invalid_path'
    if type(line) is not int or line <= 0: return None, 'invalid_line'
    matches = snapshot.get('_lookup', {}).get((path, side, line), [])
    if len(matches) != 1: return None, 'ambiguous_location' if matches else 'outside_captured_hunk'
    file, row = matches[0]
    submitted = [item for item in ranges if isinstance(item, dict) and type(item.get('start')) is int and type(item.get('end')) is int
                 and item['start'] <= row['patchStart'] and row['patchEnd'] <= item['end']]
    if not submitted: return None, 'line_not_submitted'
    return {'side':side, 'path':path, 'line':line, 'blob':file[side+'Blob'],
            'oldPath':file['oldPath'], 'newPath':file['newPath'], 'oldBlob':file['oldBlob'], 'newBlob':file['newBlob'],
            'diffSha256':snapshot['diffSha256'], 'hunkId':row['hunkId'], 'kind':row['kind'],
            'excerpt':row['text'], 'excerptSha256':row['excerptSha256'], 'newline':row['newline'],
            'chunkId':submitted[0].get('chunkId')}, None
