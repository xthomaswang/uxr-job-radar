import json
import re

import pytest

from uxr_radar.core import (Assessment, Job, experience_level, experience_metadata, messages,
    publication_decision, title_seniority, validate_assessment, anonymous_policy)
from uxr_radar.pipeline import FEED_URL, age_cell, assessment_key, handoff_assessment, location_cell, posted_age, render
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
    assert "| Level |" in output.read_text() and "Junior ⚠️<br><sub>req 3y</sub>" in output.read_text()  # ⚠️: Staff title vs 3 stated years
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


def test_large_employers_get_only_a_flame_and_repeats_collapse(tmp_path):
    jobs=[job("Conduct user interviews.").model_copy(update={"key":f"acme:{n}","source_id":str(n),"title":f"Researcher {n}","company_kind":"large"}) for n in (1,2)]
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot("acme",jobs)
    a=assessment(None)
    with store.db:
        for j in jobs:
            store.db.execute("UPDATE jobs SET assessment=?,assessment_key=? WHERE key=?",(a.model_dump_json(),assessment_key(j,{},"test"),j.key))
    output=tmp_path/"README.md";render(store,{},"test",output)
    text=output.read_text()
    assert text.count("🔥")>=2 and "LARGE" not in text  # legend + the first row's flame
    assert text.count("| ↳ |")==1


def test_multiple_locations_split_onto_lines_whatever_the_separator():
    assert location_cell("San Francisco, CA | New York City, NY | Seattle, WA")=="San Francisco, CA<br>New York City, NY<br><sub>+1 more</sub>"
    assert location_cell("New York, NY; San Francisco, CA")=="New York, NY<br>San Francisco, CA"
    assert location_cell("Remote/Hybrid - US")=="Remote/Hybrid - US"
