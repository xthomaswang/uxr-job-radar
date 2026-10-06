"""Retain validated live benchmark judgments in the normal queue (never synthetic jobs)."""
import json
import sys
from pathlib import Path
from uxr_radar.core import Job, now, validate_assessment, digest, PROMPT_VERSION
from uxr_radar.pipeline import assessment_key
from uxr_radar.store import Store

model="mlx-community/Qwen3.8-27B-8bit"
profile=json.loads(Path('config/search_policy.json').read_text())
store=Store('state/jobs.sqlite3')
for filename in sys.argv[1:]:
    payload=json.loads(Path(filename).read_text())
    if 'Qwen3.8-27B-8bit' not in payload['model_path']:
        raise ValueError('Only the evaluated primary model may populate these primary-model judgments')
    if payload.get('profile_hash') != digest(profile) or payload.get('prompt_version') != PROMPT_VERSION:
        raise ValueError('Benchmark profile/prompt differs from current configuration; reevaluate first')
    for result in payload['results']:
        if result['kind']!='live_job':continue
        row=store.db.execute('SELECT data FROM jobs WHERE key=?',(result['id'],)).fetchone()
        if not row:continue
        job=Job.model_validate_json(row['data'])
        if result.get('job_content_hash') != job.content_hash():
            continue
        key=assessment_key(job,profile,model)
        a=None;error=None
        try:a=validate_assessment(result['raw'],job)
        except ValueError as e:error=str(e)
        if result.get('generation_tokens',0)>=900:
            a=None;error='Potentially truncated model output at token limit'
        with store.db:
            if a:
                store.db.execute('UPDATE jobs SET assessment=?,assessment_key=?,assessed_at=?,error=NULL,attempts=0 WHERE key=?',(a.model_dump_json(),key,result.get('assessed_at') or now(),job.key))
            else:
                store.db.execute('UPDATE jobs SET error=?,attempts=attempts+1 WHERE key=?',(error,job.key))
            store.db.execute('INSERT INTO reviews(job_key,at,model,input_key,duration,usage,raw,error) VALUES (?,?,?,?,?,?,?,?)',(job.key,result.get('assessed_at') or now(),model,key,result['seconds'],json.dumps({'prompt_tokens':result['prompt_tokens'],'completion_tokens':result['generation_tokens'],'transport':'direct_mlx_pilot'}),result['raw'],error))
        print('Stored live assessment:' if a else 'Retained failed assessment for retry:',job.key)
