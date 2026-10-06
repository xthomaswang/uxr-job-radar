# Anonymous collection prompts

These system prompts contain only public job-search preferences and output schemas.
No applicant profile is supplied. Stage 1 classifies overall research relevance;
stage 2 extracts details from the original posting, using the first result only as
a provisional hypothesis. Failed stages are retried with validation feedback.

The runnable canonical definitions are in `src/uxr_radar/core.py`; these readable
snapshots were generated for prompt version `uxr-v6-anonymous-experience-levels`. Job documents
and previous model output remain untrusted data. The model must not generate URLs.

Experience bins use mandatory minimum years only: Junior ≤3, Mid >3 and <5, Senior ≥5 and <8, Staff ≥8, Unknown when unstated. Mid is an explicitly added intermediate bin. Preferred years and source-title seniority remain separate. All relevant levels remain in the collection. Generic capability themes broaden recall beyond UX titles without supplying a candidate biography.
