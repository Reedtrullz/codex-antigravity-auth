"""Local static reports: model text is escaped content, never HTML or actions."""
from __future__ import annotations

import html
import json
from pathlib import PurePath, PureWindowsPath

from .redaction import redact_sensitive_text, sanitize_json
from .reports import validate_report

MAX_RUNS = 20
MAX_ROWS = 200
MAX_TEXT = 8000
MAX_HTML = 4 * 1024 * 1024
CSS = '''
:root{color-scheme:light;--ink:#172c35;--muted:#425560;--paper:#f6f8fa;--line:#c5d0d5;--accent:#005f63}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:1rem/1.6 system-ui,sans-serif}
a{color:var(--accent);text-underline-offset:.2em}a:focus-visible,summary:focus-visible{outline:3px solid #9a4300;outline-offset:4px}
.skip{position:absolute;left:1rem;top:-6rem;background:white;padding:.75rem;z-index:3}.skip:focus{top:.5rem}
header,main,footer{max-width:1480px;margin:auto;padding:1.5rem clamp(1rem,3vw,3rem)}header{border-bottom:1px solid var(--line)}
h1{font-size:clamp(1.8rem,4vw,2.7rem);line-height:1.2;margin:.5rem 0}h2{font-size:1.5rem}h3{font-size:1.15rem}h4{font-size:1rem}
.eyebrow{font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:700}.muted{color:var(--muted)}
.notice{border-left:4px solid #9a4300;padding:.8rem 1rem;background:#fff5e8}.grid{display:grid;grid-template-columns:minmax(0,1fr);gap:1.5rem}
.grid.compare{grid-template-columns:repeat(2,minmax(0,1fr))}.run{min-width:0;background:white;border:1px solid var(--line);border-radius:12px;padding:1.25rem}
.run h2{margin-top:0}.states{display:flex;flex-wrap:wrap;gap:.5rem}.badge{display:inline-block;background:#edf3f5;border:1px solid var(--line);border-radius:5px;padding:.25rem .6rem;font-size:.9rem}
.badge.warn{background:#fff0db;border-color:#9a4300;color:#622900}.badge.good{background:#e2f3ed;border-color:#176344;color:#134931}
dl{display:grid;grid-template-columns:minmax(8rem,1fr) minmax(0,3fr);gap:.4rem 1rem}dt{font-weight:700}dd{margin:0;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font: .86rem/1.6 ui-monospace,monospace;max-width:100%;background:#f3f6f8;padding:.8rem;border-radius:5px}
summary{cursor:pointer;font-weight:650;padding:.65rem 0}details{border-top:1px solid var(--line);margin:.8rem 0}details[open]>summary{margin-bottom:.4rem}
.finding,.lane{border:1px solid var(--line);border-radius:7px;padding:.85rem;margin:.8rem 0}.finding h4,.lane h4{margin:.15rem 0}
ul{padding-left:1.3rem}li{overflow-wrap:anywhere}p,td,th,h1,h2,h3,h4{overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;table-layout:fixed}
caption{text-align:left;font-weight:700;margin:.5rem 0}th,td{text-align:left;vertical-align:top;padding:.65rem;border-bottom:1px solid var(--line)}thead{background:#eaf0f3}
nav ul{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;list-style:none;padding:0}.comparison{margin:1rem 0 2rem}
@media(max-width:850px){.grid.compare{grid-template-columns:minmax(0,1fr)}dl{grid-template-columns:minmax(0,1fr)}dd{margin-bottom:.4rem}.comparison th,.comparison td{padding:.4rem;font-size:.85rem}}
@media print{body{background:white}.skip,nav{display:none}.grid.compare{display:block}.run{break-inside:avoid;border-radius:0;margin-bottom:1rem}details{display:block}details>*{display:block!important}}
'''


def _mapping(value):
    return value if isinstance(value,dict) else {}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _rows(value):
    return value if isinstance(value,list) else []


def _state(value, allowed):
    return value if isinstance(value,str) and value in allowed else 'unknown'


def _display(value):
    def locations(item, depth=0):
        if depth>24:return "[Display nesting limit reached; inspect JSON]"
        if isinstance(item,dict):
            return {key:('<local path omitted>' if key in {'cwd','repo','workspace_root','resultPath','runRecordPath','rawLanePaths'}
                         else '<absolute path omitted>' if key in {'file','sourceFile','path'} and isinstance(child,str)
                            and (PurePath(child).is_absolute() or PureWindowsPath(child).is_absolute())
                         else locations(child, depth+1)) for key,child in item.items()}
        if isinstance(item,list):return [locations(child, depth+1) for child in item]
        return item
    sanitized=sanitize_json(locations(value))
    text=sanitized if isinstance(sanitized,str) else json.dumps(sanitized,sort_keys=True,ensure_ascii=False,indent=2)
    return redact_sensitive_text(text)


def escaped(value):
    if value is None:return 'Unknown / not recorded'
    text=_display(value)
    shortened=len(text)>MAX_TEXT
    text=text[:MAX_TEXT]
    return html.escape(text,quote=True)+(f'\n[Display shortened at {MAX_TEXT:,} characters; source artifact unchanged.]' if shortened else '')


def badge(label, value, *, good=()):
    value=value if isinstance(value,str) else 'unknown'
    style='good' if value in good else 'warn'
    return '<span class="badge '+style+'">'+html.escape(label)+': '+escaped(value)+'</span>'


def _details(title, value, *, opened=False):
    return '<details'+(' open' if opened else '')+'><summary>'+html.escape(title)+'</summary><pre>'+escaped(value)+'</pre></details>'


def _table(pairs):
    return '<dl>'+''.join('<dt>'+html.escape(label)+'</dt><dd>'+escaped(value)+'</dd>' for label,value in pairs)+'</dl>'


def _view(run_id, **values):
    return {'runId':run_id,**values}


def from_publication(bundle):
    record=bundle['record'];result=_mapping(bundle.get('result'));metadata=_mapping(record.get('metadata'))
    verification=_mapping(result.get('verification'))
    source={key:metadata[key] for key in ('sourceCommit','source_commit','sourceHash','source_hash','git_diff_base','git_diff_head') if key in metadata}
    coverage={'resultCoverage':_mapping(result.get('coverage')),
              'indexCounts':{key:record[key] for key in ('omittedFileCount','omittedChunkCount') if key in record},
              'indexScopeEvidence':{key:metadata[key] for key in ('declared_files','included_files','omitted_files','omitted_items','coverage','chunk_coverage') if key in metadata}}
    contract=_mapping(metadata.get('findings'))
    parser_total=_count(contract.get('findings_total'))
    parser_dropped=_count(contract.get('findings_dropped'))
    findings=[]
    for item in _rows(result.get('findings')):
        # Saved advisory/model fields cannot create a local adjudication.
        finding=_mapping(item)
        findings.append({'findingKey':finding.get('findingKey') or finding.get('id'), 'verdict':'unresolved',
                         'claimVerification':'unverified','advisory':finding,'adjudication':None})
    return _view(record.get('id'),timestamp=record.get('created_at'),
        recordHash=bundle.get('indexSha256'),sourceIdentity=source or None,publication=record.get('publicationStatus','unknown'),
        lifecycle=_state(record.get('runStatus'),{'running','success','partial','failed','interrupted'}),
        scope=_state(record.get('scopeStatus'),{'complete','partial'}),panel=result.get('panelStatus') or metadata.get('panel_status') or 'unknown',
        retention=record.get('save_output') or 'legacy_unknown',contentComplete=_mapping(result.get('retention')).get('contentComplete'),
        verification=_state(verification.get('status'),{'not_run','completed_no_evidence','tool_checks','unknown'}),
        verificationEvidence=verification,requested=result.get('requestedModels') or record.get('models'),
        actual=result.get('actualModels') or metadata.get('actual_models'),providers=result.get('actualProviders') or metadata.get('actual_providers'),
        coverage=coverage,lanes=_rows(result.get('lanes')),findings=findings,declaredFindings=_count(metadata.get("findings_count")),
        disagreements=_rows(result.get('disagreements')),unverifiable=_rows(result.get('unverifiable')),
        output=result.get('output_text') if 'output_text' in result else result.get('output_preview'),
        caveats=_rows(result.get('caveats')),media=result.get('media_coverage') or metadata.get('media_coverage'),
        errors=result.get('failureDiagnostics') or record.get('error'),
        parserLoss=('unknown' if parser_dropped is None else 'loss' if parser_dropped else 'none'),
        parserTotal=parser_total,parserDropped=parser_dropped,
        localVerdicts='Not joined: use reflection HTML export to view explicit local adjudications.')


def from_review_report(report):
    validate_report(report)
    views=[]
    for run in report['runs']:
        context=_mapping(run.get('coverage'));verification=_mapping(context.get('verification'))
        findings_contract=_mapping(context.get('findings'))
        source={key:context[key] for key in ('sourceCommit','source_commit','sourceHash','source_hash','git_diff_base','git_diff_head') if key in context}
        views.append(_view(run.get('runId'),timestamp=run.get('timestamp'),recordHash=run.get('sourceRecordHash'),sourceIdentity=source or None,
            publication='Not assessed (reflection export)',lifecycle=_state(context.get('runStatus'),{'running','success','partial','failed','interrupted'}),
            scope=run['scopeStatus'],panel=run.get('panelStatus') or 'unknown',retention=run.get('retention') or 'unknown',contentComplete=run['contentComplete'],
            verification=_state(verification.get('status'),{'not_run','completed_no_evidence','tool_checks','unknown'}),verificationEvidence=verification,
            requested=run.get('requestedModels'),actual=run.get('actualModels'),providers=run.get('actualProviders'),coverage=context,
            lanes=_rows(context.get('panel_results')),findings=run['findings'],declaredFindings=run.get('declaredFindingCount'),
            disagreements=_rows(findings_contract.get('disagreements')),unverifiable=_rows(findings_contract.get('unverifiable')),
            output=findings_contract.get('summary'),caveats=_rows(context.get('caveats')),media=context.get('media_coverage'),errors=context.get('failure_diagnostics'),
            parserLoss=run.get('parserLossStatus'),parserTotal=run.get('parserFindingTotal'),parserDropped=run.get('parserFindingsDropped'),localVerdicts='Explicit local verdicts remain separate from model claims and file checks.'))
    return views


def _collection(title, rows, render):
    body='<section><h3>'+html.escape(title)+'</h3>'
    if not rows:return body+'<p class="muted">No retained entries. This does not establish that no issue exists.</p></section>'
    body+='<p class="muted">Showing '+str(min(MAX_ROWS,len(rows)))+' of '+str(len(rows))+' retained entries.</p>'
    body+=''.join(render(value,index) for index,value in enumerate(rows[:MAX_ROWS],1))
    if len(rows)>MAX_ROWS:body+='<p class="notice">Additional retained entries are not displayed. Inspect the JSON artifact for the full collection.</p>'
    return body+'</section>'


def _finding(value,index):
    row=_mapping(value);advisory=_mapping(row.get('advisory'))
    verdict=_state(row.get('verdict'),{'confirmed','rejected','unresolved'})
    return '<article class="finding"><h4>Finding '+str(index)+': '+escaped(advisory.get('claim'))+'</h4>'+badge('Local verdict',verdict,good=('confirmed','rejected'))+badge('Model claim','unverified')+_table([
        ('Finding key',row.get('findingKey')),('Severity claimed',advisory.get('severity')),('Location',{'file':advisory.get('file'),'line':advisory.get('line')}),
        ('Source hash recorded with finding',row.get('sourceHash'))])+_details('Model evidence',advisory.get('evidence'),opened=True)+_details('File checks (not claim verification)',advisory.get('checks'))+_details('Local adjudication evidence',row.get('adjudication'),opened=True)+'</article>'


def _lane(value,index):
    row=_mapping(value);generation=_mapping(row.get('generation'))
    return '<article class="lane"><h4>Lane '+str(index)+'</h4>'+badge('Outcome',row.get('status') or 'unknown',good=('success',))+_table([
        ('Requested model',row.get('requested_model') or row.get('requestedModel')),('Actual model',row.get('actual_model') or row.get('actualModel') or generation.get('actual_model')),
        ('Actual provider',row.get('provider') or generation.get('actual_provider')),('Fallback used',row.get('fallback_used'))])+_details('Retained lane evidence',row,opened=True)+'</article>'


def render(views, *, title='Anti local run report'):
    if not isinstance(views,list) or not 1<=len(views)<=MAX_RUNS:
        raise ValueError('HTML report requires one to20 runs; select a run or comparison pair')
    if any(not isinstance(view,dict) for view in views):raise ValueError('Invalid HTML report model')
    parts=['<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'none\'; connect-src \'none\'; img-src \'none\'; font-src \'none\'; base-uri \'none\'; form-action \'none\'">',
        '<meta name="referrer" content="no-referrer"><title>'+html.escape(title)+'</title><style>'+CSS+'</style></head><body>',
        '<a class="skip" href="#main">Skip to report content</a><header><p class="eyebrow">Anti · local evidence</p><h1>'+html.escape(title)+'</h1>',
        '<p>Read-only snapshot. Model claims, local verdicts, file checks and publication consistency are separate evidence.</p>',
        '<p class="notice">Partial, degraded, unverified and unknown states remain visible. A successful run is not proof of complete coverage or correct findings.</p>',
        '<nav aria-label="Report runs"><ul>'+''.join('<li><a href="#run-'+str(i)+'">Run '+str(i)+': '+escaped(v.get('runId'))+'</a></li>' for i,v in enumerate(views,1))+'</ul></nav></header><main id="main" tabindex="-1">']
    if len(views)==2:
        parts.append('<section class="comparison" aria-labelledby="compare-title"><h2 id="compare-title">Side-by-side comparison</h2><p>Values are displayed as recorded. Absence of a finding does not mean it was resolved; differing source or scope identities do not establish an equivalent review.</p><table><caption>Recorded state comparison</caption><thead><tr><th scope="col">Field</th><th scope="col">Run 1</th><th scope="col">Run 2</th></tr></thead><tbody>')
        for label,key in [('Run identity','runId'),('Source identity','sourceIdentity'),('Lifecycle','lifecycle'),('Scope','scope'),('Panel integrity','panel'),('Verification','verification'),('Retention','retention')]:
            parts.append('<tr><th scope="row">'+label+'</th>'+''.join('<td>'+escaped(view.get(key))+'</td>' for view in views)+'</tr>')
        parts.append('</tbody></table></section>')
    parts.append('<div class="grid'+(' compare' if len(views)==2 else '')+'">')
    for index,view in enumerate(views,1):
        parts.append('<article class="run" id="run-'+str(index)+'" aria-labelledby="run-title-'+str(index)+'"><h2 id="run-title-'+str(index)+'">Run '+str(index)+': '+escaped(view.get('runId'))+'</h2><div class="states">')
        for label,key,good in [('Lifecycle','lifecycle',('success',)),('Scope','scope',('complete',)),('Panel','panel',()),('Verification','verification',()),('Retention','retention',('full',))]:
            parts.append(badge(label,view.get(key),good=good))
        parts.append('</div>'+_table([('Recorded timestamp',view.get('timestamp')),('Source identity',view.get('sourceIdentity') or None),('Record SHA-256',view.get('recordHash')),
            ('Publication consistency',view.get('publication')),('Content retained completely',view.get('contentComplete')),('Requested models',view.get('requested')),
            ('Actual models',view.get('actual')),('Actual providers',view.get('providers')),('Declared findings',view.get('declaredFindings')),('Retained findings',len(_rows(view.get('findings')))),('Parser loss',view.get('parserLoss')),('Parser finding total',view.get('parserTotal')),('Parser findings dropped',view.get('parserDropped'))]))
        parts.append('<p class="notice">'+escaped(view.get('localVerdicts'))+'</p>')
        for label,key in [('Coverage and omissions','coverage'),('Verification evidence','verificationEvidence'),('Media coverage (independent of code scope)','media'),('Caveats','caveats'),('Failure diagnostics','errors')]:
            parts.append(_details(label,view.get(key),opened=key in {'coverage','verificationEvidence'}))
        parts.append(_collection('Findings',_rows(view.get('findings')),_finding))
        parts.append(_collection('Reviewer lanes',_rows(view.get('lanes')),_lane))
        parts.append(_details('Disagreements',view.get('disagreements'),opened=True)+_details('Unverifiable claims',view.get('unverifiable'),opened=True)+_details('Retained output or preview',view.get('output'))+'</article>')
    parts.append('</div></main><footer><p>Generated locally from retained evidence. No scripts, network resources, editable fields or executable verification actions are included. Credential redaction is best-effort; inspect content before sharing.</p></footer></body></html>')
    result=''.join(parts)
    if len(result.encode('utf-8'))>MAX_HTML:raise ValueError('HTML report exceeds4MiB; export fewer runs or inspect JSON')
    return result


def report_html(report):
    return render(from_review_report(report))
