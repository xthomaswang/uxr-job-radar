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
`/v1/chat/completions` host. The default is the shared loopback endpoint
`http://127.0.0.1:8013/v1`; `scripts/serve-local.sh` binds the same port, so it
fails instead of starting a second copy when another project already serves it.
Requests are serial; this client does not load a second copy of the model. The
optional MLX server binds only to loopback and is intended for local development,
with no production authentication or priority scheduler. Other projects and their
backend configuration are not modified.

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
saved stages survive a crash. Due retries are serviced whenever a worker runs: the
background analyzer below, or a manual command. A manual review waits up to ten
minutes for the analyzer to finish its current batch. Replacing weights behind the
same model alias requires a new alias or prompt version to invalidate cached
assessments.

An unreachable model host (connection refused, timeout before connecting, HTTP
502/503/504) pauses the run without charging the job an attempt. A host that drops
the connection mid-request charges that job once and puts it on retry cooldown, so
a request that crashes a shared server is not replayed in a tight loop.

### Optional relevance ordering (CLM)

The analyzer can order its queue with
[CLM-v0.1-8B](https://huggingface.co/Contrastive-LM/CLM-v0.1-8B) (Apache-2.0): two
small projection heads over frozen Qwen3-8B last-token embeddings. `screen.py` runs
it in-process with MLX on the cached `mlx-community/Qwen3-8B-8bit`, asks one yes/no
question per posting ("mainly UX research or research-adjacent work on people,
customers or products?") and uses P(yes) as a score. The score only reorders work:
a score of at least 0.5 or 0.2 can lift a posting into the first or second title
tier, a direct research title is never demoted, and every posting still receives
the validated Qwen assessment. Scores are cached per content hash and screen
version, so changed postings are rescored.

The heads live in `~/Developer/ml_source/clm/`; `scripts/convert_clm_heads.py`
documents the one-time download and torch-free-runtime conversion. Without the heads
or the `inference` extra, the worker falls back to title ordering; `--no-screen`
disables it. `uxr-radar screen --limit 0` precomputes scores for the whole queue
without a model host. The encoder needs about 9 GB while scoring and is released
when no unscored posting remains. On a small stratified sample judged by the
production model (2026-10-06), this ordering reached about 85% of relevant postings
within the first 20% of the queue, against about 47% for title terms alone; a hard
cutoff at 0.2 would have dropped an estimated 13% of relevant postings, so the score
only orders work. `scripts/clm_eval.py` reruns the evaluation locally.

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

## Background service

Three per-user LaunchAgents run the pipeline unattended (no sudo, no cloud LLM):

| Agent | Schedule | Work |
|---|---|---|
| `io.github.xthomaswang.uxr-radar.collector` | at login, then hourly | `uxr-radar collect`: fetch due sources (6/24-hour source minima still apply); new and changed postings enter the queue |
| `io.github.xthomaswang.uxr-radar.analyzer` | always on, restarted at most once a minute | `uxr-radar worker`: CLM ordering, then serial staged review in batches of 10, retries first; waits 5 minutes when idle |
| `io.github.xthomaswang.uxr-radar.publisher` | hourly, first run one hour after load | `uxr-radar publish`: recheck links older than 6 hours, render, audit, commit and push |

```sh
uv sync --extra inference
.venv/bin/uxr-radar agents install    # uses UXR_LLM_BASE_URL / UXR_LLM_MODEL from the shell
uv run uxr-radar status               # components, queue, sources, Git, agents, model host
tail -f state/logs/analyzer.log       # rotating logs; *.launchd.log only receives crashes
.venv/bin/uxr-radar agents uninstall  # stop and remove every installed agent
```

launchd reads neither shell profiles nor `.envrc`; the installed plists carry PATH,
`HF_HOME`, `HF_HUB_OFFLINE=1`, the model endpoint and model name, and run the
project's `.venv/bin/uxr-radar` from the repository root. Reinstall after moving the
repository or changing the endpoint. macOS lists the agents under Login Items &
Extensions → Allow in the Background.

The analyzer keeps the 27B model resident only while it has work. When jobs are
due and nothing answers on the loopback endpoint, it starts `scripts/serve-local.sh`
itself, and stops that server after 10 minutes without due jobs
(`--idle-stop-minutes`), on battery, or when the agent stops. A host it did not
start, such as another project's, is used but never stopped. On a loopback port it
also reads the listening `mlx_lm.server` command line: `mlx_lm.server` reloads
weights when a request names a different model, so the analyzer sends nothing when
the shared host serves another model.

`mlx_lm.server` has no request priorities. Instead, before each job and between CLM
chunks the analyzer checks for other processes connected to the model port (for
example a game client) and pauses while any are connected, so their requests go
first; at most one in-flight request (typically 10–30 seconds) can overlap.
`uxr-radar status` lists the PIDs it is yielding to. A client that keeps an idle
connection open also pauses the analyzer; `--no-yield` disables this.

Model work (CLM scoring and Qwen) runs only on AC power (`--allow-battery` to
override); the collector and publisher are light and keep running. While the
analyzer has due work on AC power, it holds a `caffeinate -s` assertion and
releases it when idle. A closed lid, sleep, logout or a missing network still pause
the pipeline; the queue resumes afterwards. The collector skips a run when no job
host is reachable, so an outage does not consume source intervals.

The publisher commits only `README.md` and `positions.jsonl`. It builds the commit in
a temporary index from HEAD plus those two files, runs `scripts/audit_public.py`
(with `state/publication-denylist.json` when present) on exactly that tree, then
commits with the `Uxr-Radar-Publish: auto` trailer. It commits when the content
changes materially (observation timestamps are ignored) and otherwise refreshes at
least every six hours. It never force-pushes, merges or rebases. It skips or blocks,
recording the reason in `uxr-radar status`, when:

- source, config, prompt or audit files have uncommitted changes (published output
  must come from committed code);
- HEAD is not `main`, a merge/rebase is in progress, or `user.email` is not a GitHub
  noreply address;
- `origin/main` has commits HEAD lacks, or HEAD has unpushed commits that the
  publisher did not create (push or drop them manually);
- no source succeeded in the last three hours, or more than half are failing;
- the audit fails (no commit is made).

A failed push keeps the local commit and is retried on the next run. Generated
files belong to the publisher; do not edit them by hand. Run `uxr-radar publish
--dry-run` to see the audited diff without committing, or `--no-push` to commit
locally only. Do not run `watch` while the agents are installed.

Throughput depends on the shared host. With a dedicated host the earlier benchmark
measured about 17.5 seconds per single-stage judgment. On 2026-10-06, sharing the
8013 host with another project's long reasoning batch, two relevant postings took
about 3.5 minutes each across both stages. The initial backlog therefore takes days,
not hours; the README pending count shows progress. Nothing here sends
notifications or submits applications. Do not interpret the processed sample as the
entire job market.
