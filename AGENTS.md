# Project conventions

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
- Large employers get a visible README marker and priority; keep startups included.
- Use uv, a project .venv and uv.lock. Keep shared model weights outside the repo.
- Do not modify unrelated model hosts, projects or global environments.
- Benchmark reports must include hardware, macOS build and Xcode version, and
  distinguish selected/synthetic cases from independent quality measurements.
- Generate positions.jsonl with README for downstream agents. Keep publication
  metadata, input versions, source timestamps and validation status explicit.
- Before any public push, audit the staged tree and reachable Git history for
  applicant data and secrets. Never make private historical profile data public.
