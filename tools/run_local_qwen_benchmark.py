"""Context-batched benchmark runner for local llama-server."""
from __future__ import annotations
import argparse,json,os,statistics,time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import httpx

DEFAULT_DATASET=Path('benchmarks/engineering_translation_100.jsonl')
DEFAULT_RESULTS=Path('benchmarks/results/qwen3.5-9b-q4_k_m-context-batch5.jsonl')
DEFAULT_SUMMARY=Path('benchmarks/results/qwen3.5-9b-q4_k_m-context-batch5-summary.md')
MODEL_ALIAS='qwen3.5-9b-q4_k_m'

def load_dataset(path=DEFAULT_DATASET):
    rows=[]
    for n,line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(),1):
        if not line.strip(): raise ValueError(f'blank line {n}')
        try: row=json.loads(line)
        except json.JSONDecodeError as e: raise ValueError(f'invalid JSON line {n}: {e}') from e
        if not isinstance(row,dict): raise ValueError(f'line {n} is not object')
        rows.append(row)
    return rows

def batches(records,max_items=5,max_chars=1200):
    groups={}
    for r in records: groups.setdefault((r['source_language'],r['target_language'],r['category']),[]).append(r)
    for group in groups.values():
        batch=[]; chars=0
        for r in group:
            if batch and (len(batch)>=max_items or chars+len(r['source_text'])>max_chars): yield batch; batch=[]; chars=0
            batch.append(r); chars+=len(r['source_text'])
        if batch: yield batch

def _terms(batch):
    seen=[]
    for r in batch:
        for t in r.get('required_terms',[]):
            x=f"{t['source']} -> {t['target']}"
            if x not in seen: seen.append(x)
    return ', '.join(seen) or '(none)'

def build_batch_messages(batch,errors=()):
    r=batch[0]
    system=(f"Source language: {r['source_language']}. Target language: {r['target_language']}. "
            f"Engineering category: {r['category']}. Translate only. Return keys exactly as provided. "
            "Preserve numbers, units, chainages, standard numbers, and placeholders. "
            f"Use required terminology found in this batch: {_terms(batch)}. "
            "Return JSON only; no explanation and no Markdown code fences.")
    user=json.dumps({x['id']:x['source_text'] for x in batch},ensure_ascii=False,indent=2)
    if errors: user += '\nPrevious validation errors: '+ '; '.join(errors)+'\nReturn corrected JSON only.'
    return system,user

def validate_mapping(batch,raw):
    checks={r['id']:{'json':False,'id':False,'translation':False,'protected_literals':False,'required_terms':False} for r in batch}
    try: parsed=json.loads(raw)
    except (json.JSONDecodeError,TypeError): return {},['invalid_json'],checks
    if not isinstance(parsed,dict): return {},['not_object'],checks
    expected={r['id'] for r in batch}; errors=[]
    if set(parsed)!=expected: errors.append('id_set_mismatch')
    valid={}
    for r in batch:
        ident=r['id']; c=checks[ident]; c['json']=True; c['id']=ident in parsed; val=parsed.get(ident)
        c['translation']=isinstance(val,str) and bool(val.strip())
        if not c['translation']: errors.append(f'{ident}:empty_translation')
        if isinstance(val,str):
            c['protected_literals']=all(val.count(x)==r['source_text'].count(x) for x in r.get('protected_literals',[]))
            c['required_terms']=all(t['target'] in val for t in r.get('required_terms',[]))
            if not c['protected_literals']: errors.append(f'{ident}:protected_literal_mismatch')
            if not c['required_terms']: errors.append(f'{ident}:required_term_missing')
            if all(c.values()): valid[ident]=val
    return valid,errors,checks

def _content(response):
    return response.json()['choices'][0]['message']['content']

def _atomic(path,rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    temp.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8'); os.replace(temp,path)

def _record(r,raw,val,count,errors,checks,elapsed,batch_retry=False,split=False):
    return {'id':r['id'],'category':r['category'],'source_language':r['source_language'],'target_language':r['target_language'],'raw_output':raw,'translation':val,'request_count':count,'success':not errors,'error_types':sorted(set(errors)),'elapsed_seconds':round(elapsed,3),'checks':checks,'reference_similarity':round(SequenceMatcher(None,val,r['reference_translation']).ratio(),4) if val else None,'similarity_is_reference_only':True,'needs_manual_review':True,'batch_retries':int(batch_retry),'split_to_single':split}

def run_records(records,client,result_path,*,max_records=None,model_alias=MODEL_ALIAS,progress_every=10):
    path=Path(result_path); existing={}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip(): existing[json.loads(line)['id']]=json.loads(line)
    target=records[:max_records] if max_records else records; done=0
    for batch in batches(target):
        pending=[r for r in batch if existing.get(r['id'],{}).get('success') is not True]
        if not pending: done+=len(batch); continue
        start=time.perf_counter(); errors=[]; raw=''; valid={}; checks={}; attempts=0
        for attempt in range(2):
            attempts+=1; system,user=build_batch_messages(pending,errors)
            try:
                response=client.post('/v1/chat/completions',json={'model':model_alias,'messages':[{'role':'system','content':system},{'role':'user','content':user}],'temperature':0,'seed':42,'max_tokens':256,'stream':False}); response.raise_for_status(); raw=_content(response); valid,errors,checks=validate_mapping(pending,raw)
            except Exception as exc: errors=[str(exc) or 'request_error']; valid={}
            if not errors: break
        for r in pending:
            val=valid.get(r['id'])
            if errors:
                one_raw=''; one_errors=[]; one_val=None; one_checks={}; one_start=time.perf_counter(); one_attempts=0
                for _ in range(2):
                    one_attempts+=1; system,user=build_batch_messages([r],one_errors)
                    try:
                        response=client.post('/v1/chat/completions',json={'model':model_alias,'messages':[{'role':'system','content':system},{'role':'user','content':user}],'temperature':0,'seed':42,'max_tokens':256,'stream':False}); response.raise_for_status(); one_raw=_content(response); one_valid,one_errors,one_checks=validate_mapping([r],one_raw); one_val=one_valid.get(r['id'])
                    except Exception as exc: one_errors=[str(exc) or 'request_error']; one_val=None
                    if not one_errors: break
                existing[r['id']]=_record(r,one_raw,one_val,attempts+one_attempts,one_errors,one_checks,time.perf_counter()-one_start,True,True)
            else: existing[r['id']]=_record(r,raw,val,attempts,[] if val else ['validation_failed'],checks.get(r['id'],{}),time.perf_counter()-start,attempts>1,False)
            _atomic(path,[existing[x['id']] for x in target if x['id'] in existing])
        done+=len(batch)
        if progress_every and done%progress_every==0: print(f'progress: {done}/{len(target)} processed',flush=True)
    return [existing[r['id']] for r in target if r['id'] in existing]

def summarize(records,results):
    ok=[r for r in results if r.get('success')]; bad=[r for r in results if not r.get('success')]; t=[r.get('elapsed_seconds',0) for r in results]; p95=statistics.quantiles(t,n=20,method='inclusive')[18] if len(t)>1 else (t[0] if t else 0)
    lines=['# Local Qwen context-batch benchmark','',f'- Records: {len(results)}; success: {len(ok)}; failed: {len(bad)}',f'- Batch retries: {sum(r.get("batch_retries",0) for r in results)}; split-to-single records: {sum(bool(r.get("split_to_single")) for r in results)}',f'- Total elapsed: {sum(t):.3f}s; average: {statistics.mean(t) if t else 0:.3f}s; median: {statistics.median(t) if t else 0:.3f}s; P95: {p95:.3f}s','- Similarity is a reference-only indicator, not translation quality or accuracy.','','## Failures']
    lines += [f'- {r["id"]}: {", ".join(r.get("error_types",[]))}' for r in bad] or ['- none']
    return '\n'.join(lines)+'\n'

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--base-url',default='http://127.0.0.1:8088'); p.add_argument('--dataset',type=Path,default=DEFAULT_DATASET); p.add_argument('--results',type=Path,default=DEFAULT_RESULTS); p.add_argument('--summary',type=Path,default=DEFAULT_SUMMARY); p.add_argument('--limit',type=int); p.add_argument('--timeout',type=float,default=120); p.add_argument('--model-alias',default=MODEL_ALIAS); a=p.parse_args(argv); records=load_dataset(a.dataset)
    with httpx.Client(base_url=a.base_url,timeout=a.timeout) as c: results=run_records(records,c,a.results,max_records=a.limit,model_alias=a.model_alias)
    a.summary.parent.mkdir(parents=True,exist_ok=True); a.summary.write_text(summarize(records,results),encoding='utf-8'); print(json.dumps({'processed_results':len(results),'success':sum(r.get('success') is True for r in results),'failed':sum(not r.get('success') for r in results)},ensure_ascii=False)); return 0 if len(results)==len(records) else 2
if __name__=='__main__': raise SystemExit(main())
