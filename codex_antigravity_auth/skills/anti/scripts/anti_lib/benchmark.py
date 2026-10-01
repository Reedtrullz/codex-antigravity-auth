"""Offline review replay scoring. Never execute inputs or dispatch model requests."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .errors import AntiError
from .redaction import redact_sensitive_text

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ARMS = 8
MAX_FINDINGS = 32
SHA = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}')
EFFORTS = {'none','minimal','low','medium','high','xhigh','max'}
SEVERITIES = {'critical','high','medium','low','info'}


class BenchmarkError(AntiError):
    pass


def require(condition, message='Invalid benchmark input shape'):
    if not condition:raise BenchmarkError(message)


def digest(value):
    raw=value if isinstance(value,bytes) else (value.encode('utf-8') if isinstance(value,str)
        else json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'))
    return hashlib.sha256(raw).hexdigest()


def object_fields(value, required, optional=()):
    require(isinstance(value,dict) and set(required) <= set(value) <= set(required)|set(optional))


def string(value, limit=512):
    require(isinstance(value,str) and 0<len(value)<=limit and all(ord(c)>=32 and not 0xd800<=ord(c)<=0xdfff for c in value))


def identifier(value):
    require(isinstance(value,str) and ID.fullmatch(value) is not None)


def sha(value):
    require(isinstance(value,str) and SHA.fullmatch(value) is not None)


def integer(value, limit=2**53-1, *, nullable=False):
    require(nullable and value is None or type(value) is int and 0<=value<=limit)


def _pairs(pairs):
    value={}
    for key,item in pairs:
        require(key not in value,'Duplicate benchmark JSON key')
        value[key]=item
    return value


def read_json(path):
    """Bounded regular-file reads; content is data, never a command or plugin."""
    try:
        path=Path(path)
        before=path.lstat()
        require(stat.S_ISREG(before.st_mode) and not path.is_symlink() and before.st_size<=MAX_FILE_BYTES,
                'Benchmark input must be a regular file no larger than 4 MiB')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        with os.fdopen(fd,'rb') as stream:
            opened=os.fstat(stream.fileno())
            require(stat.S_ISREG(opened.st_mode) and (before.st_dev,before.st_ino)==(opened.st_dev,opened.st_ino))
            raw=stream.read(MAX_FILE_BYTES+1)
            after=os.fstat(stream.fileno())
        final=path.lstat()
        identity=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        require(len(raw)<=MAX_FILE_BYTES and identity(before)==identity(after)==identity(final),'Benchmark input changed while reading')
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,
                         parse_constant=lambda _:require(False,'Non-finite benchmark number'))
        return value
    except (OSError,UnicodeError,ValueError,RecursionError) as exc:
        raise BenchmarkError('Cannot read a bounded benchmark JSON file') from exc


def corpus():
    raw=read_json(Path(__file__).with_name('review_corpus.json'))
    object_fields(raw,('schemaVersion','id','labelBasis','holdoutVisibility','cases'))
    require(type(raw['schemaVersion']) is int and raw['schemaVersion']==1)
    identifier(raw['id']);require(isinstance(raw['cases'],list) and 1<=len(raw['cases'])<=32)
    result=deepcopy(raw);seen=set()
    for case in result['cases']:
        object_fields(case,('id','split','contract','function','before','after','defect','probes'))
        identifier(case['id']);identifier(case['function']);require(case['id'] not in seen);seen.add(case['id'])
        require(case['split'] in {'development','holdout'});string(case['contract'],2000)
        require(all(isinstance(case[key],str) and 0<len(case[key])<=16000 for key in ('before','after')))
        require(isinstance(case['probes'],list) and 1<=len(case['probes'])<=32)
        for probe in case['probes']:
            object_fields(probe,('args','expected'));require(isinstance(probe['args'],list))
        if case['defect'] is not None:
            object_fields(case['defect'],('id','severity','claim'));identifier(case['defect']['id'])
            require(case['defect']['severity'] in SEVERITIES);string(case['defect']['claim'])
        path=case['id']+'.py'
        diff=''.join(difflib.unified_diff(case['before'].splitlines(True),case['after'].splitlines(True),
                                        fromfile='a/'+path,tofile='b/'+path))
        prompt='Review this candidate for concrete defects against the stated contract. Return file, line, claim and severity.\n\nContract: '+case['contract']+'\n\nCandidate '+path+':\n'+case['after']+'\nPatch:\n'+diff
        scope={'files':[path],'complete':True,'omittedFiles':[]}
        case.update(file=path,diff=diff,prompt=prompt,baselineSha256=digest(case['before']),candidateSha256=digest(case['after']),
                    sourceSha256=digest({'file':path,'before':case['before'],'after':case['after'],'diff':diff}),
                    promptSha256=digest(prompt),scope=scope,scopeSha256=digest(scope),verificationSha256=digest(case['probes']))
    result['corpusSha256']=digest(raw)
    result['sourceRevisionKind']='sha256_fixture_snapshot'
    return result


def template(data):
    """Unexecuted template; it cannot imply that any route was available."""
    settings={'effort':'medium','maxCalls':1,'maxInputEstimate':8192,'maxOutputTokens':2048}
    rows=[]
    for case in data['cases']:
        rows.append({'caseId':case['id'],'sourceSha256':case['sourceSha256'],'promptSha256':case['promptSha256'],
            'scope':deepcopy(case['scope']),'scopeSha256':case['scopeSha256'],'status':'unavailable','submittedCalls':0,
            'requestedOutputTokens':0,'latencyMs':None,'estimatedInputTokens':None,'observedInputTokens':None,
            'observedOutputTokens':None,'findings':[]})
    return {'schemaVersion':1,'kind':'anti-benchmark-replay','corpusSha256':data['corpusSha256'],
            'matchedSettings':settings,'arms':[{'id':name,'model':'operator-selected-model','provider':'operator-selected-provider',
                'settings':deepcopy(settings),'cases':deepcopy(rows)} for name in ('arm-a','arm-b')]}


def validate_settings(value):
    object_fields(value,('effort','maxCalls','maxInputEstimate','maxOutputTokens'))
    require(isinstance(value['effort'],str) and value['effort'] in EFFORTS)
    for key,limit in (('maxCalls',100),('maxInputEstimate',10**9),('maxOutputTokens',10**7)):
        integer(value[key],limit);require(value[key]>0)


def validate_replay(value):
    object_fields(value,('schemaVersion','kind','corpusSha256','matchedSettings','arms'))
    require(type(value['schemaVersion']) is int and value['schemaVersion']==1 and value['kind']=='anti-benchmark-replay')
    sha(value['corpusSha256']);validate_settings(value['matchedSettings'])
    require(isinstance(value['arms'],list) and 1<=len(value['arms'])<=MAX_ARMS)
    arm_ids=set()
    for arm in value['arms']:
        object_fields(arm,('id','model','provider','settings','cases'));identifier(arm['id'])
        require(arm['id'] not in arm_ids,'Duplicate benchmark arm');arm_ids.add(arm['id'])
        string(arm['model'],128);string(arm['provider'],128);validate_settings(arm['settings'])
        require(isinstance(arm['cases'],list) and len(arm['cases'])<=32)
        case_ids=set()
        for row in arm['cases']:
            object_fields(row,('caseId','sourceSha256','promptSha256','scope','scopeSha256','status','submittedCalls',
                               'requestedOutputTokens','latencyMs','estimatedInputTokens','observedInputTokens','observedOutputTokens','findings'))
            identifier(row['caseId']);require(row['caseId'] not in case_ids,'Duplicate arm case');case_ids.add(row['caseId'])
            for key in ('sourceSha256','promptSha256','scopeSha256'):sha(row[key])
            require(isinstance(row['status'],str) and row['status'] in {'completed','failed','unavailable'})
            for key in ('submittedCalls','requestedOutputTokens'):integer(row[key])
            for key in ('latencyMs','estimatedInputTokens','observedInputTokens','observedOutputTokens'):integer(row[key],nullable=True)
            scope=row['scope'];object_fields(scope,('files','complete','omittedFiles'));require(type(scope['complete']) is bool)
            for key in ('files','omittedFiles'):
                require(isinstance(scope[key],list) and len(scope[key])<=32)
                for path in scope[key]:string(path,128)
                require(len(set(scope[key]))==len(scope[key]))
            require(isinstance(row['findings'],list) and len(row['findings'])<=MAX_FINDINGS)
            finding_ids=set()
            for finding in row['findings']:
                object_fields(finding,('id','file','line','claim','severity'))
                identifier(finding['id']);require(finding['id'] not in finding_ids,'Duplicate finding id');finding_ids.add(finding['id'])
                string(finding['file'],128);integer(finding['line'],100000);require(finding['line']>0)
                string(finding['claim'],2000);require(isinstance(finding['severity'],str) and finding['severity'] in SEVERITIES)


def adjudications(value, replay, data):
    object_fields(value,('schemaVersion','kind','corpusSha256','records'))
    require(type(value['schemaVersion']) is int and value['schemaVersion']==1 and value['kind']=='anti-benchmark-adjudications')
    require(value['corpusSha256']==data['corpusSha256'],'Adjudication corpus identity mismatch')
    require(isinstance(value['records'],list) and len(value['records'])<=MAX_ARMS*32*MAX_FINDINGS)
    arms={arm['id']:arm for arm in replay['arms']};cases={case['id']:case for case in data['cases']};out={}
    for item in value['records']:
        object_fields(item,('armId','caseId','findingId','findingSha256','sourceSha256','verdict','defectId','reviewer','evidence'))
        for key in ('armId','caseId','findingId'):identifier(item[key])
        key=(item['armId'],item['caseId'],item['findingId']);require(key not in out,'Duplicate local adjudication')
        arm=arms.get(key[0]);case=cases.get(key[1]);require(arm is not None and case is not None,'Unknown adjudication target')
        row=next((row for row in arm['cases'] if row['caseId']==key[1]),None)
        finding=next((f for f in row['findings'] if f['id']==key[2]),None) if row else None
        require(finding is not None,'Unknown adjudicated finding')
        require(item['findingSha256']==digest(finding) and item['sourceSha256']==case['sourceSha256'],'Adjudication evidence identity mismatch')
        string(item['reviewer'],128)
        require(item['reviewer'].casefold() not in {arm['model'].casefold(),arm['provider'].casefold()},'Model/provider identity cannot adjudicate itself')
        require(isinstance(item['verdict'],str) and item['verdict'] in {'confirmed','rejected','unresolved'})
        if item['defectId'] is not None:identifier(item['defectId'])
        require(item['verdict']=='confirmed' or item['defectId'] is None)
        evidence=item['evidence'];object_fields(evidence,('kind','detail','sha256','independent'))
        require(isinstance(evidence['kind'],str) and evidence['kind'] in {'source_inspection','reproduction'})
        string(evidence['detail'],4000);require(evidence['independent'] is True and evidence['sha256']==digest(evidence['detail']),
                                            'Independent local evidence is required')
        out[key]=item
    return out


def _sum(values):
    return {'knownTotal':sum(value for value in values if value is not None),'unknownCount':sum(value is None for value in values)}


def metrics(rows):
    valid=[row for row in rows if row['status']=='scored']
    return {'scoredCases':len(valid),'inconclusiveCases':sum(row['status']=='inconclusive' for row in rows),
            'verifiedDetections':sum(row['detections'] for row in valid),'expectedDefects':sum(row['expectedDefects'] for row in valid),
            'falseNegatives':sum(row['expectedDefects']-row['detections'] for row in valid),
            'verifiedFalsePositives':sum(row['falsePositives'] for row in valid),
            'noDefectControls':sum(row['expectedDefects']==0 for row in valid),
            'cleanControls':sum(row['expectedDefects']==0 and row['falsePositives']==0 for row in valid),
            'duplicateDetections':sum(row['duplicateDetections'] for row in valid),
            'verifiedSeverity':dict(sum((Counter(row['verifiedSeverity']) for row in valid),Counter())),
            'unresolvedFindings':sum(row.get('unresolvedFindings',0) for row in rows),
            'latencyMs':_sum([row['latencyMs'] for row in valid]),
            'estimatedInputTokens':_sum([row['estimatedInputTokens'] for row in valid]),
            'observedInputTokens':_sum([row['observedInputTokens'] for row in valid]),
            'observedOutputTokens':_sum([row['observedOutputTokens'] for row in valid])}


def evaluate(replay, local, data=None):
    data=data or corpus();validate_replay(replay)
    require(replay['corpusSha256']==data['corpusSha256'],'Replay corpus identity mismatch')
    labels={case['id']:case for case in data['cases']};judged=adjudications(local,replay,data)
    arms=[];audits=[]
    for arm in replay['arms']:
        rows=[];by_id={row['caseId']:row for row in arm['cases']}
        settings_match=arm['settings']==replay['matchedSettings']
        unknown_cases=sorted(set(by_id)-set(labels))
        for case_id,case in labels.items():
            row=by_id.get(case_id);reasons=[]
            if row is None:
                rows.append({'caseId':case_id,'split':case['split'],'status':'invalid','reasons':['missing_case']});continue
            if not settings_match:reasons.append('effort_or_budget_mismatch')
            if unknown_cases:reasons.append('unknown_case')
            for key in ('sourceSha256','promptSha256','scopeSha256'):
                if row[key]!=case[key]:reasons.append(key+'_mismatch')
            if row['scope']!=case['scope'] or digest(row['scope'])!=row['scopeSha256']:reasons.append('coverage_mismatch')
            settings=arm['settings']
            if row['submittedCalls']>settings['maxCalls'] or row['requestedOutputTokens']>settings['maxOutputTokens']:
                reasons.append('attempt_or_output_budget_exceeded')
            if row['estimatedInputTokens'] is not None and row['estimatedInputTokens']>settings['maxInputEstimate']:
                reasons.append('input_estimate_budget_exceeded')
            if row['status']=='completed' and (row['submittedCalls']<1 or row['requestedOutputTokens']<1 or row['estimatedInputTokens'] is None):
                reasons.append('missing_completed_attempt_evidence')
            if row['status']=='unavailable' and (row['submittedCalls'] or row['findings']):reasons.append('unavailable_has_execution')
            if any(f['file']!=case['file'] or f['line']>len(case['after'].splitlines()) for f in row['findings']):
                reasons.append('finding_outside_scope')
            result={'caseId':case_id,'split':case['split'],'sourceSha256':case['sourceSha256'],'promptSha256':case['promptSha256'],
                    'scopeSha256':case['scopeSha256'],'omittedFiles':len(row['scope']['omittedFiles']),
                    'status':'invalid' if reasons else row['status'],'reasons':reasons,
                    **{key:row[key] for key in ('latencyMs','estimatedInputTokens','observedInputTokens','observedOutputTokens','submittedCalls','requestedOutputTokens')}}
            if reasons or row['status']!='completed':rows.append(result);continue
            hits=set();rejected=0;unresolved=0;duplicates=0;finding_receipts=[]
            expected=case['defect']['id'] if case['defect'] else None
            for finding in row['findings']:
                annotation=judged.get((arm['id'],case_id,finding['id']))
                verdict=annotation['verdict'] if annotation else 'unresolved'
                receipt={'findingId':finding['id'],'findingSha256':digest(finding),'verdict':verdict}
                if annotation:
                    receipt.update(reviewerSha256=digest(annotation['reviewer']),evidenceSha256=annotation['evidence']['sha256'])
                if verdict=='confirmed':
                    if expected is not None and annotation['defectId']==expected:
                        duplicates+=int(expected in hits);hits.add(expected)
                    else:
                        unresolved+=1;receipt['verdict']='label_conflict'
                        audits.append({'kind':'label_conflict','armId':arm['id'],'caseId':case_id,'findingId':finding['id']})
                elif verdict=='rejected':rejected+=1
                else:unresolved+=1
                finding_receipts.append(receipt)
            result.update(status='inconclusive' if unresolved else 'scored',detections=len(hits),expectedDefects=int(expected is not None),
                          falsePositives=rejected,duplicateDetections=duplicates,unresolvedFindings=unresolved,findings=finding_receipts,
                          verifiedSeverity={case['defect']['severity']:len(hits)} if hits else {})
            rows.append(result)
        status=('invalid' if any(row['status']=='invalid' for row in rows) else
                'unavailable' if all(row['status']=='unavailable' for row in rows) else
                'scored' if all(row['status']=='scored' for row in rows) else 'inconclusive')
        arms.append({'id':arm['id'],'model':redact_sensitive_text(arm['model']),'provider':redact_sensitive_text(arm['provider']),
                     'status':status,'settings':deepcopy(arm['settings']),'cases':rows,
                     'development':metrics([r for r in rows if r['split']=='development']),
                     'publicHoldout':metrics([r for r in rows if r['split']=='holdout'])})
    comparisons=[]
    for index,left in enumerate(arms):
        for right in arms[index+1:]:
            eligible=[a['caseId'] for a,b in zip(left['cases'],right['cases']) if a['status']==b['status']=='scored']
            comparable=bool(eligible) and left['status']!='invalid' and right['status']!='invalid'
            if not comparable:eligible=[]
            for a,b in zip(left['cases'],right['cases']):
                if a['caseId'] in eligible and (a['detections'],a['falsePositives'])!=(b['detections'],b['falsePositives']):
                    audits.append({'kind':'arm_disagreement','caseId':a['caseId'],'arms':[left['id'],right['id']]})
            comparisons.append({'arms':[left['id'],right['id']],'status':'matched_subset' if comparable else 'inconclusive',
                                'matchedCases':eligible,'excludedCases':[name for name in labels if name not in eligible],
                                'results':{arm['id']:{split:metrics([r for r in arm['cases'] if r['caseId'] in eligible and r['split']==split])
                                                     for split in ('development','holdout')} for arm in (left,right)}})
    return {'schemaVersion':1,'kind':'anti-benchmark-report','mode':'offline_replay','corpusId':data['id'],
            'corpusSha256':data['corpusSha256'],'replaySha256':digest(replay),'adjudicationsSha256':digest(local),
            'status':'invalid' if any(a['status']=='invalid' for a in arms) else 'scored' if len(arms)>1 and all(a['status']=='scored' for a in arms) else 'inconclusive',
            'groundTruthBasis':data['labelBasis'],'holdoutVisibility':data['holdoutVisibility'],
            'liveExecutionVerified':False,'localEvidenceIsAttested':True,'billingObserved':False,'routingChanged':False,
            'arms':arms,'comparisons':comparisons,'audits':audits,
            'limitations':['Public synthetic fixture labels do not establish general model quality.',
                'Replay and local evidence are attributed inputs, not independently observed provider execution.',
                'Missing adjudication and label conflicts are inconclusive; no automatic winner or routing change.']}
