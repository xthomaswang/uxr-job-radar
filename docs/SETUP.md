# Setup and operation

A public, anonymous, high-recall collector for UX and adjacent research jobs.
Downstream people or agents handle applications using their own private profiles.

## Install and use the local model

Use Python 3.13 and `uv sync`; the project keeps a `.venv` and `uv.lock`.
For the optional model server, run `uv sync --extra inference`. Model weights are
external to the repository. The supplied server script uses the existing shared
Hugging Face cache and does not download weights in offline mode.

```sh
# Shell 1, only when an equivalent shared model server is not running
zsh scripts/serve-local.sh

# Shell 2, from the project root
uv run uxr-radar run --limit 50
uv run uxr-radar watch --interval 3600 --limit 50
```

Set `UXR_LLM_BASE_URL` and `UXR_LLM_MODEL` in `.envrc` to reuse a local compatible
`/v1/chat/completions` host. Requests are serial; this client does not load a second
copy of the model. The optional MLX server binds only to loopback and is intended
for local development, with no production authentication or priority scheduler.
Other projects and their backend configuration are not modified.

## Stages and retries

Every returned job is queued for local LLM assessment. Stage 1 examines the whole
posting and classifies broad relevance with a source quote. Clearly unrelated
roles finish after this stage; relevant and uncertain roles continue to Stage 2.
Stage 2 extracts mandatory and preferred experience, employment, requirements and
short original quotes. The first result is provisional context, never evidence.
Only the original job document can support extracted facts.

A failed stage gets up to three immediate requests by default, with validation
feedback, a larger token budget when needed and modest sampling variation. After
that, the same stage remains in the persistent retry queue with exponential
cooldown (30 seconds up to six hours). The worker gives due retries a slot before
new work and interleaves them, so a large backlog does not starve retries. Cached
successful overview results are reused when only details failed. Retries do not
guarantee success; source-only records stay visible while validation is pending.

```sh
uv run uxr-radar fetch
uv run uxr-radar review --limit 50 --attempts-per-stage 3
uv run uxr-radar retry --due --limit 10
uv run uxr-radar retry --force --job-key SOURCE:ID --limit 1
uv run uxr-radar verify
uv run uxr-radar render
uv run pytest -q
```

`--job-key` is repeatable for targeted validation. `--limit` counts jobs attempted,
not HTTP requests. Changing job content, anonymous policy, model name or prompt
invalidates the stage cache. A process lock prevents overlapping inference workers;
saved stages survive a crash. Due retries are serviced when a worker runs, not by
a hidden daemon. Replacing weights behind the same model alias requires a new alias
or prompt version to invalidate cached assessments.

## Anonymous policy and experience bands

`config/search_policy.json` contains only role targets, generic research/analytics
capabilities and collection preferences. The public prompt snapshots are in
`prompts/`; canonical runtime prompts live in `src/uxr_radar/core.py`.
No candidate name, school, employer history, citizenship or personal URL is used.

Targets include UXR, mixed-methods research, behavioral/social research, consumer
and market insights, product/CX analytics, research operations, human factors,
service/design strategy and research-adjacent program evaluation. All plausible
related roles stay in the pool; title keywords only order the queue.

| Experience level | Explicit mandatory minimum |
|---|---|
| Junior | At most 3 years |
| Mid | Above 3 and below 5 years; an explicit intermediate bin |
| Senior | At least 5 and below 8 years |
| Staff | At least 8 years |
| Unknown | No usable explicit mandatory number |

Mandatory years, preferred years and title seniority are separate. A Staff title
with a 5-year requirement keeps both facts and a conflict label; missing years do
not become zero. Degree/experience alternatives remain unresolved when no single
minimum can be extracted without an applicant profile. Relevant Senior/Staff roles
are retained as stretch opportunities. Large companies receive **★ LARGE** priority
markers; startups and AI startups remain included.

## Sources and costs

`config/sources.json` is the explicit coverage boundary: 48 employer sources and
five public aggregators in this release. ATS support includes Greenhouse, Ashby,
Lever, SmartRecruiters, Workday and Recruitee, plus Google and Amazon career search.
Some sources use scoped broad searches rather than every role at the employer.

See [source coverage](SOURCES.md) and [third-party API access](THIRD_PARTY_APIS.md)
for limits and official documentation. No paid API, trial subscription or API-key
account is required by the enabled configuration. Local inference still consumes
GPU time, memory and power. Expensive detail-based sources refresh no more often
than every six hours; aggregators follow their documented/configured cadence.
Arbeitnow was rate-limited in validation and is recorded as a source failure;
it must complete a later scan before a complete snapshot can be accepted.

A complete snapshot updates membership. A failed/incomplete scan preserves prior
state. Rolling feeds (for example latest-only or last-seven-days feeds) never close
older jobs just because they leave the current feed. Old records lose the recent
membership flag rather than falsely claiming closure. Source and link checks older
than 24 hours are not shown as verified.

## Public handoff and privacy

`render` writes README plus `positions.jsonl`; see [handoff contract](HANDOFF.md).
Free-text model reasons, notes and uncertainties remain local. Public `notes` and
`uncertainties` arrays are empty with `narrative_status` set to
`omitted_unverified_model_narrative`; this does not imply eligibility is clear.
Source quotes and structured labels are retained, but excerpts are not a complete
eligibility check. Downstream reviewers must read the original posting.

URLs and IDs come from source adapters, never the LLM. Schema and exact-quote checks
catch output errors but do not prove semantic correctness. An aggregator listing
can be verified without verifying the employer's application page; explicit
`verification_scope` and `employer_verified` fields preserve that distinction.
Each aggregator record credits and links to its provider.

Before publishing, stage only public files and run:

```sh
uv run python scripts/audit_public.py
# Optional: an ignored local denylist; it is never included in Git
uv run python scripts/audit_public.py --denylist state/publication-denylist.json
# Audit the cleaned reachable Git history as well
uv run python scripts/audit_public.py --history
```

The audit rejects private profile fields, raw state, credentials, stale generated
policy/prompt metadata and applicant-specific narratives. Local SQLite, raw feeds,
model outputs and weights are ignored. Public history is initialized from the
sanitized tree, with no prior private profile commit attached.

`watch` is a foreground loop; stop it with Ctrl-C. It is not installed as a login
service. A sleeping Mac cannot process jobs. Local updates do not automatically
push to GitHub, send notifications or submit applications. Large initial backlogs
remain visible; do not interpret the processed sample as the entire job market.
