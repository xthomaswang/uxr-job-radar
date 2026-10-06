# Project conventions

> Using the job feed, for example to apply? Start at [docs/HANDOFF.md](docs/HANDOFF.md).
> This file is for contributors changing the code.

- This is a public, anonymous job-discovery repository. Commit only generic search
  policy, prompts, source adapters and job facts. Never add an applicant biography,
  names, schools, portfolio links, resume, contact details or applicant-specific notes.
- Keep credentials, local snapshots, databases, raw model output and weights out of Git.
- Every fetched public position enters the on-device model queue. Title patterns
  may order the queue but must not reject jobs before model assessment.
- Optimize relevance recall. Retain related roles with experience/qualification gaps;
  a human or another AI handles applications. Do not infer applicant eligibility.
- Model failures must be retried with feedback and persistent scheduling. Failed
  outputs never become validated facts; retain source-only records meanwhile.
- The LLM must never generate or rewrite links. Preserve source IDs and URLs;
  validate current feed membership and page status independently.
- Missing experience is unknown, not zero. Preferred experience is not mandatory.
- Large employers keep queue priority and a `company_kind` label; only the curated FAANG+ list (`pipeline.FAANG_PLUS`) gets the 🔥 README marker. Keep startups included.
- Employers a maintainer judges not to be direct openings are listed in the ignored `state/excluded-employers.json` and omitted at render time (no cache impact). Never name them or the reasons in public files; the README gives only a count.
- Sponsorship and U.S.-citizenship flags come from fixed rules over the exact posting text, never the model; they must prefer a missed flag to a wrong one (an affirmative "we do sponsor" suppresses the flag).
- Use uv, a project .venv and uv.lock. Keep shared model weights outside the repo.
- Do not modify unrelated model hosts, projects or global environments.
- Benchmark reports must include hardware, macOS build and Xcode version, and
  distinguish selected/synthetic cases from independent quality measurements.
- Generate positions.jsonl with README for downstream agents. Keep publication
  metadata, input versions, source timestamps and validation status explicit.
- Before any public push, audit the staged tree and reachable Git history for
  applicant data and secrets. Never make private historical profile data public.
- The background publisher commits only README.md and positions.jsonl, audits the
  exact commit tree first, and never force-pushes, merges, rebases or pushes commits
  it did not create. Published output must come from committed code and config.
- A model-host outage must not consume job attempts. Never send a model name other
  than the one a shared local host serves: mlx_lm.server would reload weights and
  evict another project's model.
- Judgments from other weights share the cache only through `core.MODEL_ALIASES`
  and must record the exact weights. Results computed elsewhere are untrusted: import
  them only after local re-validation against unchanged job content.
