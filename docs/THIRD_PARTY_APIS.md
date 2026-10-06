# Public job APIs

These integrations use public endpoints without accounts, keys, subscriptions or per-request charges. They complement company career feeds; they do not cover every job board or guarantee real-time employer availability. All returned positions enter the local-model queue, including unrelated titles. API-side search scopes are explicit.

| Provider | Retrieved scope | Scheduled minimum interval | Primary documentation |
|---|---|---:|---|
| Remotive | Public active remote listings, all returned records | 6 hours | [Official API repository](https://github.com/remotive-com/remote-jobs-api) |
| Arbeitnow | Every advertised page of the public Europe/Germany feed, including regional listings | 6 hours | [Free API](https://www.arbeitnow.com/blog/job-board-api), [API terms](https://www.arbeitnow.com/terms) |
| Remote OK | Latest public feed; not an exhaustive inventory | 6 hours | [Official feed FAQ](https://remoteok.com/faq) |
| Jobicy | All cursor pages within the last seven publication days | 6 hours | [Official API and fair-use rules](https://github.com/Jobicy/remote-jobs-api) |
| Himalayas | All advertised search pages for `UX research` and `human factors`; cross-query overlap deduplicated | 24 hours | [Official API reference](https://himalayas.app/docs/remote-jobs-api), [API overview](https://himalayas.app/api) |

Configuration is in [aggregator_sources.json](../config/aggregator_sources.json). Pagination requests are sequential with a one-second pause, or two seconds for Arbeitnow. A failed request, repeated cursor, untrusted pagination destination, inconsistent page identity or page cap raises an error instead of publishing a partial snapshot. HTTP 429 remains a fetch failure; the previous data survives for a later scheduled pass. Do not manually loop fetch commands to defeat provider intervals.

## Attribution and link meaning

Every record preserves the provider's original listing URL, ID and name. README and JSONL retain attribution and link back to the provider. No employer application URL is synthesized. A verified aggregator page means that listing was reachable and matched its source identity; it does **not** independently verify the employer's application page. Downstream applicants should follow the provider's application link and check employer identity and availability.

Remotive requires credit and its canonical links, delays its public feed by 24 hours, recommends at most four fetches daily, and restricts redistribution to other third-party job boards or email/signup-gated listings. Its paid private API is not used. [Source](https://github.com/remotive-com/remote-jobs-api)

Arbeitnow's public API needs no key and is free; its terms require a link back and reserve the right to revoke access. The feed reports hourly updates. Its optional custom paid API is not used. [API](https://www.arbeitnow.com/blog/job-board-api), [terms](https://www.arbeitnow.com/terms)

Remote OK explicitly offers free unauthenticated feeds and requires provider credit plus original posting links. Its API notice also prohibits unapproved logo use; this project uses provider names, not logos. The six-hour interval is this project's conservative default, not a published service guarantee. [FAQ](https://remoteok.com/faq), [feed notice](https://remoteok.com/api)

Jobicy permits discovery products and AI tools, requires original attribution and canonical URLs, and forbids automated synchronization more often than hourly. Sequential cursor requests complete one pass. Only its free public API is used. Older jobs disappearing from its seven-day feed must not be called closed. [Official documentation](https://github.com/Jobicy/remote-jobs-api)

Himalayas permits internal dashboards and AI workflows with attribution and canonical links; its API is cached daily and can return 429. It restricts submitting its jobs to other third-party job boards. The full feed was over 117,000 records when checked, so this project uses documented public keyword search rather than a large full-feed crawl. [API reference](https://himalayas.app/docs/remote-jobs-api), [usage conditions](https://himalayas.app/api)

## Completeness and observed limitations

On October 5, 2026 (America/New_York), one validation pass retrieved Remotive 17 jobs, Remote OK 99, Jobicy 713 across four pages, and Himalayas 825 unique jobs across 44 pages. These are observed feed counts, not counts of relevant UXR jobs or employer-verified openings. Arbeitnow reached page 18 before returning HTTP 429 during validation; no partial snapshot was accepted. Its adapter is enabled with slower pagination, but a successful complete live pass is still pending.

Himalayas search advertised 829 indexed hits for `UX research` but materialized 802 job records across all 42 page offsets; `human factors` produced 30 records across two pages, with seven overlapping jobs. Raw metadata preserves indexed and returned counts separately. All advertised page ranges were traversed; missing index entries were not fabricated, and this is not an exhaustive inventory claim. Queries can be expanded in configuration, at the cost of additional API requests and local inference work.

Arbeitnow repeats regional records across pages. The adapter deduplicates matching IDs, titles, employers and canonical URLs while retaining the latest supplied description; conflicting identities fail the pass.

One Remote OK record had no readable description after removing image markup. It remains queued with an empty description and its original title/URL; the scraper does not invent missing duties. Raw snapshots stay local.

Remote OK and Jobicy use rolling membership. Old observed jobs survive feed rollover, but stale observations are not presented as fresh feed verification. Separate link checks and downstream application review are still necessary. Cross-provider duplicates are currently retained with separate provenance; downstream tools should deduplicate before applying.

Paid APIs, free trials requiring billing, API-key services and restricted employer products are not silently enabled. This configuration deliberately makes a bounded, reproducible no-cost integration rather than claiming access to every third-party API.
