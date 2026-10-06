import json
import re

import pytest

from uxr_radar.core import (Assessment, Job, experience_level, experience_metadata, messages,
    publication_decision, title_seniority, validate_assessment, anonymous_policy)
from uxr_radar.pipeline import FEED_URL, Exclusions, age_cell, assessment_key, faang_plus, handoff_assessment, load_exclusions, location_cell, place_label, posted_age, posting_flags, render
from uxr_radar.store import Store


def assessment(years=None, **changes):
    value=dict(decision="recommend",role="uxr",required_years=years,
        experience="not_stated" if years is None else "at_least_3" if years>=3 else "under_3",
        employment="full_time",reason="Relevant research responsibilities.",notes=[],uncertainties=[],
        evidence=[{"field":"role","quote":"Conduct user interviews."}])
    value.update(changes)
    return Assessment.model_validate(value)


@pytest.mark.parametrize("years,expected",[(None,"unknown"),(0,"junior"),(2,"junior"),(3,"junior"),
    (3.5,"mid"),(4,"mid"),(4.99,"mid"),(5,"senior"),(7,"senior"),(7.99,"senior"),(8,"staff"),(12,"staff")])
def test_mutually_exclusive_mandatory_experience_bins(years,expected):
    assert experience_level(years)==expected


@pytest.mark.parametrize("years",[None,0,3,4,5,8,12])
def test_every_relevant_experience_level_stays_in_pool(years):
    assert publication_decision(assessment(years))!="reject"
    assert publication_decision(assessment(years,role="adjacent_research",decision="review"))!="reject"


def test_title_seniority_and_preferred_years_never_invent_mandatory_years():
    a=assessment(preferred_years=8,evidence=[{"field":"preferred_experience","quote":"8 years of research is preferred."}])
    out=handoff_assessment(a,"Staff UX Researcher")
    assert out["experience_level"]=="unknown" and out["required_years"] is None
    assert out["preferred_years"]==8 and out["title_seniority"]=="staff"
    assert out["seniority_conflict"] is False


def test_title_conflict_is_labeled_without_rewriting_source_or_numeric_level():
    out=handoff_assessment(assessment(2),"Senior UX Researcher")
    assert out["experience_level"]=="junior" and out["title_seniority"]=="senior"
    assert out["required_years"]==2 and out["seniority_conflict"] is True
    assert "Both source facts" in out["seniority_note"]
    assert title_seniority("Associate Director of Research")=="lead"


def job(description):
    return Job(key="acme:1",source="acme",source_id="1",company="Acme",company_kind="large",
        title="UX Researcher",location="Worldwide",url="https://jobs.example.com/1",description=description)


def test_mandatory_and_preferred_years_have_separate_validated_evidence():
    j=job("Conduct user interviews. Requires 3 years of research. 5 years is preferred.")
    a=assessment(3,preferred_years=5,evidence=[{"field":"experience","quote":"Requires 3 years of research."},
        {"field":"preferred_experience","quote":"5 years is preferred."}])
    validated=validate_assessment(a.model_dump_json(),j)
    out=handoff_assessment(validated,j.title)
    assert out["experience_level"]=="junior" and out["required_years"]==3 and out["preferred_years"]==5


def test_preferred_years_cannot_use_an_unquoted_or_other_field_number():
    j=job("Requires 3 years of research. 8 years is preferred.")
    a=assessment(3,preferred_years=8,evidence=[{"field":"experience","quote":"Requires 3 years of research."}])
    with pytest.raises(ValueError,match="preference number"):
        validate_assessment(a.model_dump_json(),j)


def test_preferred_only_requirement_cannot_be_mandatory():
    j=job("Conduct user interviews. 8 years of experience is preferred.")
    a=assessment(8,evidence=[{"field":"experience","quote":"8 years of experience is preferred."}])
    with pytest.raises(ValueError,match="preference-only"):
        validate_assessment(a.model_dump_json(),j)


def test_student_status_does_not_validate_zero_experience():
    j=job("Current students welcome.")
    a=assessment(0,evidence=[{"field":"experience","quote":"Current students welcome."}])
    with pytest.raises(ValueError,match="explicit no-experience"):
        validate_assessment(a.model_dump_json(),j)


def test_readme_and_jsonl_expose_year_level_separately_from_source_title(tmp_path):
    j=job("Conduct user interviews. Requires 3 years of research.").model_copy(update={"title":"Staff UX Researcher"})
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",[j])
    a=assessment(3,evidence=[{"field":"experience","quote":"Requires 3 years of research."}])
    with store.db:
        store.db.execute("UPDATE jobs SET assessment=?,assessment_key=?,link_state='verified',checked_at=last_seen",(a.model_dump_json(),assessment_key(j,{},"test")))
    output=tmp_path/"README.md";render(store,{},"test",output)
    row=json.loads(output.with_name("positions.jsonl").read_text())
    assert row["experience_level"]=="junior" and row["title_seniority"]=="staff" and row["seniority_conflict"]
    assert row["required_years"]==3 and row["retrieval_category"]=="recommend"
    assert row["assessment"]["experience_level"]=="junior"
    assert "Staff UX Researcher ⚠️<br><sub>req 3y</sub>" in output.read_text()  # ⚠️: Staff title vs 3 stated years; the level is the section, not the row
    assert "Staff UX Researcher" in output.read_text() and "Mid >3 and <5" in output.read_text()


def test_anonymous_capability_policy_and_broad_related_roles_reach_prompt():
    policy={"targets":["program evaluation","product analytics"],"anonymous_capabilities":["surveys","R","Python","SQL"],
        "experience_levels":{"junior_max":3,"senior_min":5,"staff_min":8},"candidate_name":"Fictional Private Name"}
    content=json.dumps(messages(job("Evaluate program outcomes using surveys and SQL."),policy,stage="overview"))
    for capability in ("program evaluation","product analytics","surveys","Python","SQL"):
        assert capability in content
    assert "Fictional Private Name" not in content
    assert "anonymous_capabilities" in anonymous_policy(policy)


def test_unsupported_qualification_narrative_stays_local(tmp_path):
    j=job("Conduct user interviews. Requires 3 years of research. A master's degree is preferred.")
    invented_notes=["A master's degree is mandatory.","Requires US employment work authorization."]
    a=assessment(3,notes=invented_notes,uncertainties=["The applicant needs a doctorate."],
        evidence=[{"field":"experience","quote":"Requires 3 years of research."}])
    raw=a.model_dump_json()
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",[j])
    with store.db:
        store.db.execute("UPDATE jobs SET assessment=?,assessment_key=?",(raw,assessment_key(j,{},"test")))
    output=tmp_path/"README.md";render(store,{},"test",output)
    text=output.with_name("positions.jsonl").read_text()
    row=json.loads(text);public=row["assessment"]
    assert public["notes"]==[] and public["uncertainties"]==[]
    assert public["narrative_status"]=="omitted_unverified_model_narrative"
    assert public["evidence"]==a.model_dump()["evidence"] and public["required_years"]==3
    for value in invented_notes+a.uncertainties:
        assert value not in text+output.read_text()
    assert "Free-text model reasons, notes and uncertainties are withheld" in output.read_text()
    assert "not a complete eligibility check" in output.read_text()
    assert a.notes==invented_notes
    assert store.rows()[0]["assessment"]==raw


@pytest.mark.parametrize("quote",["1–3 years of research is preferred.","1-3 years of research is preferred.",
    "1 to 3 years of research is preferred.","1 to3 years of research is preferred."])
def test_preferred_range_is_preserved_without_scalar_upper_bound(quote):
    a=assessment(preferred_years=3,evidence=[{"field":"preferred_experience","quote":quote}])
    public=handoff_assessment(a)
    assert public["preferred_years"] is None
    assert public["preferred_years_range"]=={"min":1,"max":3}
    assert public["required_years"] is None and public["experience_level"]=="unknown"
    assert a.preferred_years==3  # raw assessment remains intact


@pytest.mark.parametrize("quote",["8+ years of research experience preferred.",
    "Lead 1-3 projects; 8 years of research experience preferred.",
    "Salary $80,000-$130,000 per year; 8 years preferred.",
    "Study children 1-3 years old; 8 years of research experience preferred.",
    "Plan the 2025-2027 years; 8 years of research experience preferred."])
def test_unrelated_ranges_do_not_override_single_preferred_years(quote):
    public=handoff_assessment(assessment(preferred_years=8,evidence=[{"field":"preferred_experience","quote":quote}]))
    assert public["preferred_years"]==8 and public["preferred_years_range"] is None


def test_mandatory_range_is_not_copied_to_preferred_range():
    public=handoff_assessment(assessment(1,evidence=[{"field":"experience","quote":"1-3 years of research required."}]))
    assert public["required_years"]==1 and public["preferred_years_range"] is None


def test_multiple_preferred_ranges_are_not_fabricated_into_one_range():
    public=handoff_assessment(assessment(preferred_years=3,evidence=[
        {"field":"preferred_experience","quote":"1-3 years of interviews preferred."},
        {"field":"preferred_experience","quote":"4-6 years of surveys preferred."}]))
    assert public["preferred_years"] is None and public["preferred_years_range"] is None
    assert public["extraction_warnings"] and len(public["evidence"])==2


def test_preferred_range_is_exported_and_rendered(tmp_path):
    quote="1-3 years of prior user research is preferred."
    j=job("Conduct user interviews. "+quote)
    a=assessment(preferred_years=3,evidence=[{"field":"preferred_experience","quote":quote}])
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",[j])
    with store.db:
        store.db.execute("UPDATE jobs SET assessment=?,assessment_key=?",(a.model_dump_json(),assessment_key(j,{},"test")))
    output=tmp_path/"README.md";render(store,{},"test",output)
    row=json.loads(output.with_name("positions.jsonl").read_text())
    assert row["preferred_years"] is None and row["preferred_years_range"]=={"min":1,"max":3}
    assert row["assessment"]["preferred_years_range"]==row["preferred_years_range"]
    assert "pref 1–3y" in output.read_text() and "pref 3y" not in output.read_text()  # a range, not the scalar 3


def test_readme_points_agents_at_the_feed_and_counts_match_it(tmp_path):
    j=job("Conduct user interviews. Requires 3 years of research.")
    a=assessment(3,evidence=[{"field":"experience","quote":"Requires 3 years of research."}])
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",[j])
    with store.db:
        store.db.execute("UPDATE jobs SET assessment=?,assessment_key=?",(a.model_dump_json(),assessment_key(j,{},"test")))
    output=tmp_path/"README.md";render(store,{},"test",output)
    text=output.read_text()
    assert "(docs/HANDOFF.md)" in text and FEED_URL in text
    counts=re.findall(r"\*\*\[[^\]]+\]\(#-[^)]*\)\*\* \((\d+)\)",text)
    assert sum(map(int,counts))==len(output.with_name("positions.jsonl").read_text().splitlines())==1
    assert text.count("<details>")==text.count("</details>")


def test_age_parses_the_source_date_formats_and_never_invents_one():
    assert posted_age(None) is None and posted_age("") is None and posted_age("not a date") is None
    for value in ("2020-01-02","2020-01-02T03:04:05Z","2020-01-02T03:04:05.123+00:00","January  2, 2020"):
        assert posted_age(value)>2000  # every supported shape parses, whatever today's date is
    assert [age_cell(d) for d in (None,0,29,30,364,365)]==["—","0d","29d","1mo","12mo","1y"]


def test_flame_marks_faang_plus_only_and_repeats_collapse(tmp_path):
    base=job("Conduct user interviews.")
    jobs=[base.model_copy(update={"key":f"acme:{n}","source_id":str(n),"title":f"Researcher {n}","company":name,"company_kind":"large"})
        for n,name in enumerate(("Amazon","Amazon","Acme Corp"))]
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",jobs)
    a=assessment(None)
    with store.db:
        for j in jobs:
            store.db.execute("UPDATE jobs SET assessment=?,assessment_key=? WHERE key=?",(a.model_dump_json(),assessment_key(j,{},"test"),j.key))
    output=tmp_path/"README.md";render(store,{},"test",output)
    text=output.read_text()
    assert text.count("| **Amazon** 🔥 |")==1 and text.count("| ↳ |")==1  # second Amazon row collapses
    assert "| **Acme Corp** |" in text and "Acme Corp** 🔥" not in text  # large, but not FAANG+
    rows={json.loads(l)["company"]:json.loads(l) for l in output.with_name("positions.jsonl").read_text().splitlines()}
    assert rows["Amazon"]["faang_plus"] is True and rows["Acme Corp"]["faang_plus"] is False and rows["Acme Corp"]["company_kind"]=="large"


def test_faang_plus_matches_names_not_substrings():
    assert all(faang_plus(n) for n in ("Amazon","Amazon.com","Google LLC","Meta","NVIDIA","OpenAI","Anthropic","Microsoft Corporation"))
    assert not any(faang_plus(n) for n in ("Apple Bank","Metabase","Elastic","Airbnb","Googleplex Staffing"))


@pytest.mark.parametrize("text,expected",[
    ("We do not offer visa sponsorship for this role.",(True,False)),
    ("Please note that visa sponsorship is not available for this position.",(True,False)),
    ("We are unable to sponsor visas at this time.",(True,False)),
    ("Candidates must be authorized to work in the US without sponsorship.",(True,False)),
    ("We do sponsor visas! However, we aren't able to successfully sponsor visas for every role and every candidate.",(False,False)),
    ("This role does not require sponsorship now or in the future.",(False,False)),
    ("Visa sponsorship is available for the right candidate.",(False,False)),
    ("You must be a U.S. citizen to apply.",(False,True)),
    ("This position requires U.S. citizenship and an active TS/SCI clearance.",(False,True)),
    ("US Citizenship required. 8+ years of experience.",(False,True)),
    ("Must be a U.S. citizen or permanent resident.",(False,False)),
    ("Open to U.S. citizens, permanent residents and visa holders. Tell us about your citizenship goals.",(False,False)),
    ("Help us citizenship workshops run smoothly.",(False,False)),
])
def test_posting_flags_read_the_text_and_prefer_missing_to_wrong(text,expected):
    assert posting_flags(text)==expected


def test_flags_reach_the_readme_and_the_feed(tmp_path):
    j=job("Conduct user interviews. We do not offer visa sponsorship. You must be a U.S. citizen.")
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",[j])
    with store.db:
        store.db.execute("UPDATE jobs SET assessment=?,assessment_key=?",(assessment(None).model_dump_json(),assessment_key(j,{},"test")))
    output=tmp_path/"README.md";render(store,{},"test",output)
    row=json.loads(output.with_name("positions.jsonl").read_text())
    assert row["sponsorship_not_offered"] is True and row["us_citizenship_required"] is True
    assert "UX Researcher 🛂 🇺🇸" in output.read_text()


def test_excluded_employers_jobs_and_aggregator_duplicates_are_omitted(tmp_path):
    base=job("Conduct user interviews.")
    official=base.model_copy(update={"key":"acme:keep","source_id":"keep","title":"UX Researcher","company":"Keep Co"})
    copy=base.model_copy(update={"key":"agg:copy","source":"agg","source_id":"copy","title":"UX  Researcher!","company":"Keep Co.","source_kind":"aggregator","source_label":"Agg"})
    other=base.model_copy(update={"key":"agg:other","source":"agg","source_id":"other","title":"Insights Lead","company":"Keep Co","source_kind":"aggregator","source_label":"Agg"})
    skip_company=base.model_copy(update={"key":"acme:c","source_id":"c","title":"Researcher C","company":"Skip Me LLC"})
    skip_job=base.model_copy(update={"key":"acme:j","source_id":"j","title":"Researcher J","company":"Other Co"})
    store=Store(tmp_path/"jobs.sqlite3")
    store.snapshot("acme",[official,skip_company,skip_job]);store.snapshot("agg",[copy,other])
    with store.db:
        for j in (official,copy,other,skip_company,skip_job):
            store.db.execute("UPDATE jobs SET assessment=?,assessment_key=? WHERE key=?",(assessment(None).model_dump_json(),assessment_key(j,{},"test"),j.key))
    listing=tmp_path/"excluded.json"
    listing.write_text(json.dumps([{"company":"skip me","reason":"private note"},{"key":"acme:j","reason":"private job note"},"Unrelated Employer"]))
    output=tmp_path/"README.md";render(store,{},"test",output,excluded=load_exclusions(listing))
    text=output.read_text();kept=sorted(json.loads(l)["key"] for l in output.with_name("positions.jsonl").read_text().splitlines())
    assert kept==["acme:keep","agg:other"]  # official kept, its aggregator copy dropped, a different aggregator posting kept
    assert "Skip Me" not in text and "private" not in text
    assert "Omitted from the lists: 2 postings a maintainer judged not to be direct openings and 1 aggregator postings that duplicate an official listing." in text
    assert load_exclusions(tmp_path/"missing.json")==Exclusions() and load_exclusions(None)==Exclusions()
