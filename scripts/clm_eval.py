#!/usr/bin/env python3
"""Evaluate CLM-v0.1-8B as a review-queue ORDERING signal; it never rejects jobs.

Labels are silver: earlier local Qwen3.8-27B role judgments (uxr/adjacent vs
non_target) and synthetic edge cases, reported separately. Unlabeled random jobs
form a background population for percentile/top-k estimates. This is a small,
selected sample, not production recall or accuracy. Reads a database copy or the
live file only through an immutable SQLite URI; outputs go to state/clm-eval/.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from uxr_radar.core import Job, PROMPT_VERSION  # noqa: E402
from uxr_radar.pipeline import priority  # noqa: E402
from uxr_radar.screen import QUESTION, QUESTION_VERSION, RELEVANT, Screener, candidates, job_context, fit_state, softmax  # noqa: E402

OUT = ROOT / "state/clm-eval"
NEGATIVE_SYNTHETIC = {"research_not_uxr", "injected_job"}
CHOICE7 = {"type": "choice", "instructions": "Which kind of work is this job mainly about?", "criteria": {
    "uxr": "User experience research: studying users through interviews, usability tests, surveys, diary or field studies, and turning findings into product insights.",
    "adjacent": "Research-adjacent work on people, customers or products: consumer or market insights, product or customer analytics, behavioral or social science research, human factors, research operations, service or design strategy, or program evaluation.",
    "engineering": "Software, data, infrastructure or machine learning engineering, or AI model research.",
    "commercial": "Sales, account management, marketing, partnerships or business development.",
    "corporate": "Recruiting, people operations, finance, accounting, legal, security or IT.",
    "operations": "Operations, logistics, customer support or administrative work.",
    "design": "Visual, brand, product or content design without user research."}}
CHOICE3 = {"type": "choice", "instructions": "Which kind of work is this job mainly about?", "criteria": {
    "uxr": CHOICE7["criteria"]["uxr"], "adjacent": CHOICE7["criteria"]["adjacent"],
    "unrelated": "Unrelated work such as software or machine learning engineering, sales, marketing, recruiting, finance, legal, operations or customer support, without research on users, customers or behavior."}}
NOUL = QUESTION  # the production screen question
CONTEXT_ONLY = {"type": "choice", "instructions": None, "criteria": CHOICE7["criteria"]}
FORMULATIONS = {"choice7": (CHOICE7, ("uxr", "adjacent"), 2048), "choice3": (CHOICE3, ("uxr", "adjacent"), 2048),
    "noul": (NOUL, RELEVANT, 2048), "context7": (CONTEXT_ONLY, ("uxr", "adjacent"), 2048),
    "choice7_1024": (CHOICE7, ("uxr", "adjacent"), 1024)}


def connect(path):
    db = sqlite3.connect(f"file:{Path(path).resolve()}?immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def live_labels(db) -> dict[str, dict]:
    """Relevance from local Qwen roles; the current assessment wins over older raw outputs."""
    labels = {}
    for r in db.execute("SELECT job_key, raw, at FROM reviews WHERE error IS NULL AND raw != '' ORDER BY at"):
        try:role = json.loads(r["raw"]).get("role")
        except ValueError:continue
        if role in {"uxr", "adjacent_research", "non_target"}:labels[r["job_key"]] = {"role": role, "from": "review"}
    for r in db.execute("SELECT key, assessment FROM jobs WHERE assessment IS NOT NULL"):
        labels[r["key"]] = {"role": json.loads(r["assessment"])["role"], "from": "assessment"}
    out = {}
    for key, label in labels.items():
        row = db.execute("SELECT data, missing FROM jobs WHERE key=?", (key,)).fetchone()
        if row:out[key] = {"job": Job.model_validate_json(row["data"]), "positive": label["role"] != "non_target", **label}
    return out


def synthetic_labels() -> dict[str, dict]:
    cases = json.loads((ROOT / "state/eval-cases.json").read_text())
    return {c["id"]: {"job": Job.model_validate(c["job"]), "positive": c["id"] not in NEGATIVE_SYNTHETIC, "role": "synthetic", "from": "eval-cases.json"}
        for c in cases if c["kind"] == "synthetic_edge_case"}


def auc(pos, neg) -> float | None:
    if not pos or not neg:return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def metrics(scores: dict[str, float], labeled: dict[str, dict], background: list[str]) -> dict:
    pos = [scores[k] for k, v in labeled.items() if v["positive"]]
    neg = [scores[k] for k, v in labeled.items() if not v["positive"]]
    bg = np.array([scores[k] for k in background])
    result = {"positives": len(pos), "negatives": len(neg), "auc": auc(pos, neg)}
    for pct in (10, 20, 30):
        cut = np.percentile(bg, 100 - pct)
        result[f"recall_top{pct}pct_of_background"] = sum(p >= cut for p in pos) / len(pos) if pos else None
    if pos:
        threshold = min(pos)
        result["threshold_keeping_all_positives"] = threshold
        result["background_fraction_below_threshold"] = float((bg < threshold).mean())
        result["negatives_below_threshold"] = sum(n < threshold for n in neg)
        result["positive_background_percentiles"] = sorted(round(float((bg < p).mean()), 4) for p in pos)
    return result


def formulation_scores(screener: Screener, name: str, jobs: dict[str, Job], cache: dict) -> dict[str, dict[str, float]]:
    """Returns {formulation: {key: score}}; raw ablation reuses the choice7 state embeddings."""
    question, relevant, max_tokens = FORMULATIONS[name]
    keys, texts = candidates(question)
    order = list(jobs)
    t = time.perf_counter()
    sequences = [fit_state(screener.encoder, job_context(jobs[k]), question.get("instructions"), max_tokens) for k in order]
    states = screener.encoder.embed_ids(sequences)
    seconds = time.perf_counter() - t
    actions_raw = screener.encoder.embed_ids([screener.encoder.encode(x) for x in texts])
    columns = [keys.index(k) for k in relevant]
    probs = softmax(screener.heads.scale * screener.heads.project("state_head", states) @ screener.heads.project("action_head", actions_raw).T)
    out = {name: dict(zip(order, probs[:, columns].sum(axis=1).tolist()))}
    if name == "choice7":
        raw = softmax(100.0 * states @ actions_raw.T)
        out["raw7_ablation"] = dict(zip(order, raw[:, columns].sum(axis=1).tolist()))
    cache[name] = {"encode_seconds": round(seconds, 2), "jobs": len(order), "tokens": sum(map(len, sequences)), "max_tokens": max_tokens}
    return out


def evaluate(args):
    db = connect(args.db)
    live, synthetic = live_labels(db), synthetic_labels()
    labeled_keys = set(live)
    active = [r["key"] for r in db.execute("SELECT key FROM jobs WHERE missing=0")]
    background = random.Random(args.seed).sample(sorted(set(active) - labeled_keys), args.background)
    jobs = {k: Job.model_validate_json(db.execute("SELECT data FROM jobs WHERE key=?", (k,)).fetchone()[0]) for k in background}
    jobs.update({k: v["job"] for k, v in live.items()})
    jobs.update({"synthetic:" + k: v["job"] for k, v in synthetic.items()})
    synthetic = {"synthetic:" + k: v for k, v in synthetic.items()}
    screener = Screener()
    timing, all_scores = {}, {}
    for name in args.formulations:
        all_scores.update(formulation_scores(screener, name, jobs, timing))
        print(json.dumps({"formulation": name, **timing[name]}), flush=True)
    all_scores["choice7+noul_mean"] = {k: (all_scores["choice7"][k] + all_scores["noul"][k]) / 2 for k in jobs} if {"choice7", "noul"} <= set(all_scores) else None
    all_scores = {k: v for k, v in all_scores.items() if v}
    all_scores["regex_priority_tier"] = {k: float(2 - priority({"data": j.model_dump_json(), "first_seen": "", "key": k})[0]) for k, j in jobs.items()}
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "database": str(args.db),
        "screen_version": screener.version, "question_version": QUESTION_VERSION, "background_jobs": len(background), "seed": args.seed,
        "label_note": "Silver labels: local Qwen3.8-27B roles from earlier prompt versions (live) and synthetic edge cases. Small selected sample; not production recall/accuracy.",
        "timing": timing, "live": {}, "synthetic": {}}
    for name, scores in all_scores.items():
        report["live"][name] = metrics(scores, live, background)
        report["synthetic"][name] = metrics(scores, synthetic, background)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "eval-scores.jsonl", "w") as f:
        for k in jobs:
            label = live.get(k) or synthetic.get(k)
            f.write(json.dumps({"key": k, "title": jobs[k].title, "label": None if label is None else label["positive"],
                "label_source": None if label is None else label["from"], **{n: round(s[k], 6) for n, s in all_scores.items()}}, ensure_ascii=False) + "\n")
    (OUT / "eval-summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("screen_version", "background_jobs", "timing")}, indent=1))
    for split in ("live", "synthetic"):
        print(f"\n[{split}] formulation | pos/neg | AUC | recall@top10/20/30% | bg cut @100% recall | neg cut")
        for name, m in report[split].items():
            fmt = lambda x: "—" if x is None else f"{x:.3f}"
            print(f"  {name:20} {m['positives']}/{m['negatives']} {fmt(m['auc'])} {fmt(m['recall_top10pct_of_background'])}/{fmt(m['recall_top20pct_of_background'])}/{fmt(m['recall_top30pct_of_background'])} {fmt(m.get('background_fraction_below_threshold'))} {m.get('negatives_below_threshold')}")
    screener.close()


def score_all(args):
    """Resumable full-backlog scoring with the default question into a local SQLite file."""
    db = connect(args.db)
    screener = Screener()
    OUT.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(args.out)
    out.execute("CREATE TABLE IF NOT EXISTS scores (job_key TEXT PRIMARY KEY, content_hash TEXT, score REAL, version TEXT, scored_at TEXT)")
    done = {(r[0], r[1]) for r in out.execute("SELECT job_key, content_hash FROM scores WHERE version=?", (screener.version,))}
    rows = [Job.model_validate_json(r["data"]) for r in db.execute("SELECT data FROM jobs WHERE missing=0")]
    todo = [j for j in rows if (j.key, j.content_hash()) not in done]
    print(json.dumps({"active": len(rows), "todo": len(todo), "version": screener.version}), flush=True)
    start, scored = time.perf_counter(), 0
    for i in range(0, len(todo), args.chunk):
        chunk = todo[i:i + args.chunk]
        scores = screener.score(chunk)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with out:
            out.executemany("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?)",
                [(j.key, j.content_hash(), s, screener.version, stamp) for j, s in zip(chunk, scores)])
        scored += len(chunk)
        elapsed = time.perf_counter() - start
        print(json.dumps({"scored": scored, "of": len(todo), "seconds": round(elapsed, 1), "s_per_job": round(elapsed / scored, 3)}), flush=True)
    screener.close()
    summarize(args, db, out, screener.version, time.perf_counter() - start, scored)


def summarize(args, db, out, version, seconds, scored):
    scores = dict(out.execute("SELECT job_key, score FROM scores WHERE version=?", (version,)).fetchall())
    jobs = {r["key"]: r for r in db.execute("SELECT key, data, first_seen FROM jobs WHERE missing=0")}
    keys = [k for k in jobs if k in scores]
    values = np.array([scores[k] for k in keys])
    deciles = np.percentile(values, [10, 20, 30, 40, 50, 60, 70, 80, 90])
    rank = {k: float((values < scores[k]).mean()) for k in keys}  # fraction scored below
    tiers = {k: priority(jobs[k])[0] for k in keys}
    live = live_labels(db)
    summary = {"version": version, "scored_this_run": scored, "wall_seconds_this_run": round(seconds, 1),
        "active_scored": len(keys), "score_deciles": [round(float(x), 6) for x in deciles],
        "fraction_above": {str(t): float((values >= t).mean()) for t in (0.01, 0.05, 0.1, 0.2, 0.5, 0.8)},
        "regex_tier_by_score_decile": {str(t): {"count": sum(1 for k in keys if tiers[k] == t),
            "in_top_decile": sum(1 for k in keys if tiers[k] == t and rank[k] >= 0.9),
            "in_bottom_decile": sum(1 for k in keys if tiers[k] == t and rank[k] < 0.1),
            "in_bottom_half": sum(1 for k in keys if tiers[k] == t and rank[k] < 0.5)} for t in (0, 1, 2)},
        "live_labeled_percentiles": {k: {"positive": v["positive"], "percentile": round(rank[k], 4)} for k, v in live.items() if k in rank}}
    top = sorted(keys, key=lambda k: -scores[k])
    summary["top_tier2_titles"] = [(json.loads(jobs[k]["data"])["title"], round(scores[k], 4)) for k in top if tiers[k] == 2][:25]
    summary["bottom_tier0_titles"] = [(json.loads(jobs[k]["data"])["title"], round(scores[k], 4)) for k in reversed(top) if tiers[k] == 0][:25]
    (OUT / "score-all-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


def wilson(k, n, z=1.96):
    if not n:return (None, None)
    p = k / n
    centre, half = (p + z * z / (2 * n)) / (1 + z * z / n), z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / (1 + z * z / n)
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def ordering_comparison(points, strata):
    """Inverse-probability-weighted comparison of queue orderings on the stratified sample.

    Each labeled job stands for population/labeled jobs of its stratum. Scores: title
    tier only, CLM only, and the production combination (tier lifted by CLM bands,
    CLM score breaking ties). Reports weighted AUC and the share of relevant jobs
    reached within the first q of the queue.
    """
    weight = {name: s["population"] / s["labeled"] for name, s in strata.items() if s["labeled"]}
    orderings = {"title_tier_only": lambda p: 2 - p[3], "clm_only": lambda p: p[2], "combined": lambda p: (2 - p[4]) + p[2]}
    result = {}
    for name, score in orderings.items():
        items = [(score(p), weight[p[0]], p[1]) for p in points]
        pos = [(s, w) for s, w, y in items if y]
        neg = [(s, w) for s, w, y in items if not y]
        wins = sum(wp * wn * ((sp > sn) + 0.5 * (sp == sn)) for sp, wp in pos for sn, wn in neg)
        total_pos, total = sum(w for _, w in pos), sum(w for _, w, _ in items)
        ranked = sorted(items, key=lambda x: -x[0])
        reach = {}
        for q in (0.1, 0.2, 0.3, 0.5):
            budget, taken, found, i = q * total, 0.0, 0.0, 0
            while i < len(ranked):  # ties are taken as a block, pro rata at the boundary
                j = i
                while j < len(ranked) and ranked[j][0] == ranked[i][0]:j += 1
                block_w = sum(w for _, w, _ in ranked[i:j])
                block_pos = sum(w for _, w, y in ranked[i:j] if y)
                share = min(1.0, (budget - taken) / block_w) if block_w else 0.0
                taken += share * block_w
                found += share * block_pos
                if share < 1.0:break
                i = j
            reach[f"top{int(q * 100)}pct"] = round(found / total_pos, 3) if total_pos else None
        result[name] = {"weighted_auc": round(wins / (sum(w for _, w in pos) * sum(w for _, w in neg)), 4) if pos and neg else None,
            "relevant_reached": reach}
    result["note"] = "Weighted to the random background population; production Qwen labels; small sample."
    return result


def stratified(args):
    """Relevance of a score-stratified random sample, judged by the production Qwen pipeline.

    Strata come from the random background population; the estimate of a cutoff's
    recall weights each stratum's relevant fraction by its size in that population.
    """
    from uxr_radar.core import Assessment, anonymous_policy, digest, publication_decision
    plan = json.loads((OUT / "stratified-sample.json").read_text())
    policy = json.loads((ROOT / "config/search_policy.json").read_text())
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    strata = {name: {"population": size, "labeled": 0, "relevant": 0, "unassessed": 0, "relevant_titles": []} for name, size in plan["strata_sizes"].items()}
    tier2_relevant = []
    points = []  # (stratum, relevant, clm score, title tier, combined tier) for ordering comparisons
    for item in plan["sample"]:
        row = db.execute("SELECT data, assessment, assessment_key FROM jobs WHERE key=?", (item["key"],)).fetchone()
        job = Job.model_validate_json(row["data"])
        stratum = strata[item["stratum"]]
        # Same key as pipeline.assessment_key, for any prompt version the labels were made with.
        if not row["assessment"] or row["assessment_key"] not in {digest([job.content_hash(), anonymous_policy(policy), args.model, v]) for v in args.prompt_version}:
            stratum["unassessed"] += 1
            continue
        relevant = publication_decision(Assessment.model_validate_json(row["assessment"])) != "reject"
        stratum["labeled"] += 1
        stratum["relevant"] += relevant
        fake_row = {"data": row["data"], "first_seen": "", "key": job.key}
        title_tier, combined_tier = priority(fake_row)[0], priority(fake_row, item["noul"])[0]
        points.append((item["stratum"], relevant, item["noul"], title_tier, combined_tier))
        if relevant:
            stratum["relevant_titles"].append((job.title, round(item["noul"], 3)))
            if title_tier == 2:
                tier2_relevant.append((job.title, round(item["noul"], 3)))
    for s in strata.values():
        s["relevant_fraction"] = round(s["relevant"] / s["labeled"], 4) if s["labeled"] else None
        s["wilson95"] = wilson(s["relevant"], s["labeled"])
        s["estimated_relevant_in_population"] = round(s["relevant_fraction"] * s["population"], 1) if s["labeled"] else None
    order = ["ge0.5", "0.2-0.5", "lt0.2"]
    est = [strata[n]["estimated_relevant_in_population"] or 0.0 for n in order]
    low_upper = (strata["lt0.2"]["wilson95"][1] or 0.0) * strata["lt0.2"]["population"]
    recall = {"cutoff_0.5": round(est[0] / sum(est), 4) if sum(est) else None,
        "cutoff_0.2": round((est[0] + est[1]) / sum(est), 4) if sum(est) else None,
        "cutoff_0.2_conservative": round((est[0] + est[1]) / (est[0] + est[1] + low_upper), 4) if est[0] + est[1] + low_upper else None}
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": args.model,
        "labels": "Production Qwen3.8-27B staged judgments (silver, not human ground truth) of a stratified random sample.",
        "strata": strata, "estimated_recall_of_cutoff": recall,
        "population_fraction_kept": {"cutoff_0.5": round(strata["ge0.5"]["population"] / sum(s["population"] for s in strata.values()), 4),
            "cutoff_0.2": round((strata["ge0.5"]["population"] + strata["0.2-0.5"]["population"]) / sum(s["population"] for s in strata.values()), 4)},
        "relevant_with_non_research_titles": tier2_relevant,
        "ordering_comparison": ordering_comparison(points, strata)}
    (OUT / "stratified-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["eval", "score-all", "summarize", "stratified"])
    p.add_argument("--model", default="mlx-community/Qwen3.8-27B-8bit", help="stratified: assessment model name")
    p.add_argument("--prompt-version", nargs="+", default=[PROMPT_VERSION], help="stratified: prompt version(s) the labels were produced with")
    p.add_argument("--db", default=str(ROOT / "state/jobs.sqlite3"), help="Read through an immutable URI; prefer a snapshot copy")
    p.add_argument("--background", type=int, default=600)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--formulations", nargs="+", default=["choice7", "choice3", "noul", "context7"], choices=list(FORMULATIONS))
    p.add_argument("--out", default=str(OUT / "scores.sqlite3"))
    p.add_argument("--chunk", type=int, default=64)
    args = p.parse_args()
    if args.command == "eval":evaluate(args)
    elif args.command == "score-all":score_all(args)
    elif args.command == "stratified":stratified(args)
    else:
        db = connect(args.db);out = sqlite3.connect(args.out)
        version = out.execute("SELECT version FROM scores ORDER BY scored_at DESC LIMIT 1").fetchone()[0]
        summarize(args, db, out, version, 0.0, 0)


if __name__ == "__main__":
    main()
