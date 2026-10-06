"""Standalone detail-stage diagnostic; not a full staged-pipeline or representative accuracy benchmark."""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from uxr_radar.core import Job, messages, validate_assessment, digest, PROMPT_VERSION, now


def main():
    p=argparse.ArgumentParser();p.add_argument("--model-path",required=True);p.add_argument("--cases",required=True);p.add_argument("--output",required=True);a=p.parse_args()
    import mlx.core as mx
    from mlx_lm import load, stream_generate
    from mlx_lm.sample_utils import make_sampler
    profile=json.loads(Path("config/search_policy.json").read_text())
    t=time.perf_counter();model,tokenizer=load(a.model_path);mx.eval(model.parameters());load_seconds=time.perf_counter()-t
    cases=json.loads(Path(a.cases).read_text());results=[]
    for case in cases:
        job=Job.model_validate(case["job"])
        prompt=tokenizer.apply_chat_template(messages(job,profile),tokenize=False,add_generation_prompt=True,enable_thinking=False)
        start=time.perf_counter();parts=[];last=None
        for last in stream_generate(model,tokenizer,prompt=prompt,max_tokens=900,sampler=make_sampler(temp=0)):
            parts.append(last.text)
        raw="".join(parts);elapsed=time.perf_counter()-start;error=None;assessment=None
        try:
            if last is None or last.generation_tokens >= 900:
                raise ValueError("Potentially truncated model output at token limit")
            assessment=validate_assessment(raw,job).model_dump()
        except Exception as e:error=str(e)
        correct=assessment is not None and assessment["decision"] in case["expected"]
        result={"job_content_hash":job.content_hash(),"assessed_at":now(),"id":case["id"],"kind":case["kind"],"expected":case["expected"],"assessment":assessment,"correct_decision":correct,"schema_and_evidence_valid":assessment is not None,"error":error,"seconds":elapsed,"prompt_tokens":last.prompt_tokens,"generation_tokens":last.generation_tokens,"prompt_tps":last.prompt_tps,"generation_tps":last.generation_tps,"peak_memory_gb":last.peak_memory,"raw":raw}
        results.append(result)
        Path(a.output).parent.mkdir(parents=True,exist_ok=True)
        Path(a.output).write_text(json.dumps({"model_path":a.model_path,"profile_hash":digest(profile),"prompt_version":PROMPT_VERSION,"load_seconds":load_seconds,"results":results},ensure_ascii=False,indent=2))
        print(json.dumps({k:result[k] for k in ['id','correct_decision','error','seconds','generation_tps','peak_memory_gb']},ensure_ascii=False),flush=True)


if __name__=="__main__":main()
