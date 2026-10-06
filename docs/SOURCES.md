# Sources and measured coverage

Validation date: **October 5, 2026 (America/New_York)**. Configuration contains **53 sources: 48 employer sources and 5 third-party feeds**. The current local snapshot contains **13,365 active source records**, from **52 successful source scans**. Arbeitnow is configured but its validation pass returned HTTP 429; partial pages were discarded. These are raw records awaiting or undergoing local-model classification, **not 13,365 UXR matches or independently verified employer openings**. The same opening can appear in more than one source.

[sources.json](../config/sources.json) is the runtime source registry. [aggregator_sources.json](../config/aggregator_sources.json) records the aggregator subset. The project collects every record returned within the declared scopes, then queues it for the on-device model. It does not remove records by title or experience before inference. Large employers have a curated **★ LARGE** priority marker; other established companies, startups and AI startups remain included.

## Official employer coverage

| Platform | Employer sources | Observed records | Scope | Minimum refresh interval |
|---|---:|---:|---|---|
| Greenhouse | 26 | 6,138 | Complete public board feed, including full descriptions | Watch cycle (default 1 hour) |
| Ashby | 12 | 1,881 | Complete listed/public board feed; unlisted postings excluded | Watch cycle (default 1 hour) |
| Lever | 2 | 150 | Complete public company postings feed | Watch cycle (default 1 hour) |
| Google Careers | 1 | 60 | Official USER_EXPERIENCE category, all advertised pages | Watch cycle (default 1 hour) |
| Amazon Jobs | 1 | 1,037 | Union of 8 declared broad searches, all advertised pages | Watch cycle (default 1 hour) |
| SmartRecruiters | 3 | 1,237 | Union of 6 declared broad searches, then full posting details | 6 hours |
| Workday | 2 | 1,192 | Union of 8 declared broad searches, then full posting details | 6 hours |
| Recruitee | 1 | 16 | Complete published offers feed including requirements | Watch cycle (default 1 hour) |

A complete board feed means the configured public ATS board, not every subsidiary, separate regional career site or internal posting of that employer. Google, Amazon, SmartRecruiters and Workday have explicitly bounded category/search coverage. No geography, seniority or employment-type filter is imposed by these source adapters.

Declared scopes:

- Google: `USER_EXPERIENCE` category; includes related design and other UX roles.
- Amazon: `UX research`, `user research`, `usability`, `human factors`, `consumer insights`, `customer insights`, `research operations`, `product research`.
- SmartRecruiters: `research`, `UX`, `insights`, `usability`, `human factors`, `service design`.
- Workday: `research`, `UX`, `usability`, `human factors`, `consumer insights`, `customer insights`, `research operations`, `product research`.

Search engines may interpret multiple words broadly. For example, NVIDIA’s research query returns many engineering/ML positions. Every returned match is preserved for model judgment; a result count is not a count of relevant UX research jobs.

### Configured employer roster

| Employer | Platform | Observed records | Priority/category |
|---|---|---:|---|
| **Adobe** | Workday | 244 | ★ LARGE |
| **Affirm** | Greenhouse | 184 | ★ LARGE |
| **Airbnb** | Greenhouse | 151 | ★ LARGE |
| Airtable | Greenhouse | 4 | established |
| **Amazon** | Amazon Jobs | 1,037 | ★ LARGE |
| Anthropic | Greenhouse | 645 | ai startup |
| **Asana** | Greenhouse | 101 | ★ LARGE |
| Back Market | Ashby | 35 | established |
| Blueprint Technologies | Greenhouse | 26 | established |
| bunq | Recruitee | 16 | established |
| Cohere | Ashby | 135 | ai startup |
| **Coinbase** | Greenhouse | 226 | ★ LARGE |
| **Coursera** | Greenhouse | 15 | ★ LARGE |
| Cursor | Ashby | 132 | ai startup |
| **Databricks** | Greenhouse | 887 | ★ LARGE |
| **Discord** | Greenhouse | 51 | ★ LARGE |
| **DoorDash** | Greenhouse | 459 | ★ LARGE |
| **Dropbox** | Greenhouse | 40 | ★ LARGE |
| **Duolingo** | Greenhouse | 60 | ★ LARGE |
| **Elastic** | Greenhouse | 402 | ★ LARGE |
| ElevenLabs | Ashby | 139 | ai startup |
| Emergent Labs | Greenhouse | 42 | ai startup |
| **Figma** | Greenhouse | 164 | ★ LARGE |
| **Google** | Google Careers | 60 | ★ LARGE |
| **HoYoverse** | Ashby | 13 | ★ LARGE |
| **Instacart** | Greenhouse | 122 | ★ LARGE |
| Linear | Ashby | 31 | startup |
| **Lyft** | Greenhouse | 189 | ★ LARGE |
| **MongoDB** | Greenhouse | 392 | ★ LARGE |
| **NielsenIQ** | SmartRecruiters | 311 | ★ LARGE |
| Notion | Ashby | 135 | established |
| **NVIDIA** | Workday | 948 | ★ LARGE |
| **Okta** | Greenhouse | 364 | ★ LARGE |
| OpenAI | Ashby | 821 | ai startup |
| Perplexity | Ashby | 129 | ai startup |
| **Pinterest** | Greenhouse | 174 | ★ LARGE |
| Ramp | Ashby | 159 | startup |
| **Reddit** | Greenhouse | 151 | ★ LARGE |
| Replit | Ashby | 70 | ai startup |
| **Roblox** | Greenhouse | 255 | ★ LARGE |
| Saviynt | Lever | 70 | established |
| Scale AI | Greenhouse | 188 | ai startup |
| **ServiceNow** | SmartRecruiters | 709 | ★ LARGE |
| **Spotify** | Lever | 80 | ★ LARGE |
| **Stripe** | Greenhouse | 718 | ★ LARGE |
| **Twilio** | Greenhouse | 128 | ★ LARGE |
| **Ubisoft** | SmartRecruiters | 217 | ★ LARGE |
| Vanta | Ashby | 82 | startup |

## Third-party feeds

These are discovery sources. A reachable provider listing does not independently verify the employer’s application page. Original provider URLs and attribution are preserved; no employer application URL is invented. See [THIRD_PARTY_APIS.md](THIRD_PARTY_APIS.md) for usage conditions and provider documentation.

| Feed | Accepted records | Validated scope / current status | Minimum interval |
|---|---:|---|---|
| [Remotive](https://github.com/remotive-com/remote-jobs-api) | 17 | Entire public active feed returned; provider delays it by 24 hours | 6 hours |
| [Arbeitnow](https://www.arbeitnow.com/blog/job-board-api) | 0 | HTTP 429 at page 18; incomplete pass rejected, complete live validation still pending | 6 hours |
| [Remote OK](https://remoteok.com/faq) | 99 | Latest rolling public feed; no full-inventory or pagination guarantee | 6 hours |
| [Jobicy](https://github.com/Jobicy/remote-jobs-api) | 713 | All four cursor pages within its last-seven-days publication window | 6 hours |
| [Himalayas](https://himalayas.app/docs/remote-jobs-api) | 825 | Union of `UX research` and `human factors`; all 44 advertised page ranges visited | 24 hours |

Himalayas advertised 829 indexed hits for `UX research`, but returned 802 materialized records across 42 pages. `human factors` returned 30 across two pages, with seven overlapping records. Both indexed and returned counts are retained; missing index rows are not invented. One Remote OK record has no readable description and stays explicitly incomplete in the model queue.

Remote OK and Jobicy have **rolling membership**: an older record disappearing from their latest feed does not prove that the job closed. Previously observed records survive rollover with their original observation timestamps. Cross-provider duplicates are retained with provenance; downstream application tools must deduplicate before applying.

## Refresh cost, failures and link meaning

All configured endpoints were tested without purchasing an API subscription, supplying an API key or creating a paid account. This describes the tested public access path, not a promise of unlimited or permanently free service. Network traffic, electricity, local inference time and maintenance still cost resources.

Single validation-pass measurements for adapters that require a detail request for each job:

| Employer | Records | Fetch + parse time, before LLM inference |
|---|---:|---:|
| ServiceNow | 709 | 292 seconds |
| Ubisoft | 217 | 89 seconds |
| NielsenIQ | 311 | 129 seconds |
| Adobe | 244 | 127 seconds |
| NVIDIA | 948 | 531 seconds |

Those sources use a six-hour minimum refresh interval. The SmartRecruiters adapter is sequential and caps its own requests below the documented default 10 requests/second. API failures and 429 responses become visible source errors; they do not close every stored job. Cooldowns also apply after failed attempts. Never repeatedly invoke fetch commands to defeat a provider interval. [SmartRecruiters rate-limit documentation](https://developers.smartrecruiters.com/docs/rate-limiting)

A watch cycle runs only while the local process and computer remain available. These intervals are scheduling minima, not guaranteed delivery latency, and this repository does not itself imply an installed always-on service. Expensive scans and inference backlogs add delay.

Complete-pagination checks reject missing/repeated pages, unexpected identities, conflicting totals or failed required details. Workday continuation pages return `total=0`; the adapter uses the initial count and rechecks the first page after pagination. Its public career-site JSON is not a versioned integration contract, so format changes fail visibly rather than silently creating an empty board.

Employer links come from authoritative source fields or deterministic official source IDs. Independent link checks preserve redirects, unavailable pages and unknown status. Strict identity checks can leave a real link unverified; for example, Recruitee’s numeric ID need not occur in its canonical career URL. Provider-level verification is not employer-level verification. An application agent should recheck the destination immediately before submission.

## Known gaps and future access changes

- This is a reproducible list of configured sources, not every website or every free API on the internet.
- Microsoft, Apple, Meta and several other major employers are not currently integrated. Separate regional boards and subsidiaries can also be missing even for a listed company.
- Tested Netflix/HubSpot/Mistral/Block endpoints were absent, empty or incomplete and were not added as successful sources. Personio probes returned disabled XML, empty descriptions or a generic-site redirect, so no Personio source is configured.
- Recruitee’s official documentation announces mandatory employer Careers Site API tokens beginning **February 10, 2027**. The currently public bunq feed may therefore require replacement or authorized access later. A future 401 remains a source error; the scraper does not bypass it. [Official authentication notice](https://docs.recruitee.com/reference/authentication-1)
- Paid APIs, billing-backed trials, account/key-gated APIs and restricted employer systems are not silently enabled.

Local validation receipts and raw snapshots are ignored by Git. The source-expansion receipt measured **25 newly added official employers and 6,343 raw records**. The public counts above are a dated observation; reproduce them with a new scheduled fetch and inspect source health, because postings and API access change over time.
