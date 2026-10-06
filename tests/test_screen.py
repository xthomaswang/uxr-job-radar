"""Model-free checks for the optional CLM ordering screen (no encoder weights loaded)."""
import json
import math

import pytest

# The screen is optional: it needs the `inference` extra (mlx, mlx-lm, numpy).
np = pytest.importorskip("numpy")
mx = pytest.importorskip("mlx.core")

from uxr_radar.core import Job
from uxr_radar.screen import (Encoder, Heads, Screener, candidates, fit_state, head_forward, job_context,
    job_text, sha256_file, softmax, state_text, to_text, QUESTION)


def reference_head(w, cfg, x):
    gelu = np.vectorize(lambda v: 0.5 * v * (1 + math.erf(v / math.sqrt(2))))
    h = gelu(x @ w["inp.weight"].T + w["inp.bias"])
    for i in range(cfg["depth"] - 2):
        z = h @ w[f"hidden.{i}.weight"].T + w[f"hidden.{i}.bias"]
        if cfg["layernorm"]:
            z = (z - z.mean(-1, keepdims=True)) / np.sqrt(z.var(-1, keepdims=True) + 1e-5)
            z = z * w[f"norms.{i}.weight"] + w[f"norms.{i}.bias"]
        z = gelu(z)
        h = h + z if cfg["residual"] else z
    out = h @ w["out.weight"].T + w["out.bias"]
    return out / np.linalg.norm(out, axis=-1, keepdims=True)


def tiny_weights(rng, hidden=8, width=6, proj=4, prefix=""):
    shapes = {"inp.weight": (width, hidden), "inp.bias": (width,), "hidden.0.weight": (width, width),
        "hidden.0.bias": (width,), "norms.0.weight": (width,), "norms.0.bias": (width,),
        "out.weight": (proj, width), "out.bias": (proj,)}
    return {prefix + k: rng.standard_normal(s).astype(np.float32) for k, s in shapes.items()}


@pytest.mark.parametrize("residual", [False, True])
def test_head_matches_numpy_reference(residual):
    rng = np.random.default_rng(0)
    cfg = {"depth": 3, "layernorm": True, "residual": residual, "activation": "gelu"}
    w = tiny_weights(rng)
    x = rng.standard_normal((3, 8)).astype(np.float32)
    got = head_forward({k: mx.array(v) for k, v in w.items()}, cfg, x)
    assert np.allclose(got, reference_head(w, cfg, x), atol=1e-5)
    assert np.allclose(np.linalg.norm(got, axis=-1), 1, atol=1e-5)


def test_upstream_text_layout():
    assert to_text({"a": 1, "b": {"c": True}}) == "a: 1\n\nb:\n  c: true"
    assert state_text("Context", "Question?") == "Context\n\nQuestion?"
    assert state_text("Context", None) == "Context"
    assert candidates({"type": "choice", "criteria": {"x": "Option text", "y": ""}}) == (["x", "y"], ["Option text", "y"])
    assert candidates({"type": "noul", "instructions": "Is it?"}) == (["false", "true"],
        ["false: No. This is false: Is it?", "true: Yes. This is true: Is it?"])
    assert candidates({"type": "score", "criteria": ["Low", "High"]}) == (["0", "1"], ["Low", "High"])
    with pytest.raises(ValueError):candidates({"type": "choice", "criteria": {}})


def test_job_text_has_source_facts_but_no_url():
    job = Job(key="a:1", source="a", company="Acme", company_kind="large", source_id="1", title="UX Researcher",
        location="Remote", url="https://jobs.example.com/1", description="Run interviews.")
    text = job_text(job)
    assert text.startswith("Job title: UX Researcher\n\nCompany: Acme") and text.endswith(QUESTION["instructions"])
    assert "https://" not in text and "Run interviews." in text


class WordTokenizer:
    def __init__(self):self.vocab = {}
    def encode(self, text):return [self.vocab.setdefault(w, len(self.vocab)) for w in text.split(" ")]
    def decode(self, ids):
        words = {i: w for w, i in self.vocab.items()}
        return " ".join(words[i] for i in ids)


class FakeEncoder(Encoder):
    def __init__(self):
        super().__init__(repo="test/encoder")
        self.tokenizer = WordTokenizer()
        self.model = object()
    def load(self):return self
    def encode(self, text):return self.tokenizer.encode(text)
    @property
    def revision(self):return "rev"
    def embed_ids(self, sequences):
        rng = np.random.default_rng(len(sequences))
        return rng.standard_normal((len(sequences), 8)).astype(np.float32)


def test_truncation_trims_context_and_keeps_question():
    enc = FakeEncoder()
    ids = fit_state(enc, {"Description": " ".join(f"w{i}" for i in range(500))}, "Which kind of work?", max_tokens=50)
    assert len(ids) <= 50
    assert enc.tokenizer.decode(ids).endswith("Which kind of work?")
    short = fit_state(enc, "Short context", "Question?", max_tokens=50)
    assert enc.tokenizer.decode(short) == "Short context\n\nQuestion?"


def write_heads(tmp_path):
    rng = np.random.default_rng(1)
    weights = {**tiny_weights(rng, prefix="state_head."), **tiny_weights(rng, prefix="action_head.")}
    path = tmp_path / "heads.safetensors"
    mx.save_safetensors(str(path), {k: mx.array(v) for k, v in weights.items()})
    meta = {"cfg": {"depth": 3, "layernorm": True, "residual": False, "activation": "gelu", "width": 6},
        "logit_scale": math.log(200.0), "safetensors_sha256": sha256_file(path)}
    path.with_suffix(".json").write_text(json.dumps(meta))
    return path


def test_screener_scores_are_probabilities_and_version_tracks_question(tmp_path):
    path = write_heads(tmp_path)
    assert Heads(path).scale == 100.0  # upstream clamps exp(logit_scale) at 100
    job = Job(key="a:1", source="a", company="Acme", company_kind="large", source_id="1", title="Analyst",
        location="Remote", url="https://jobs.example.com/1", description="Analyze survey data.")
    screener = Screener(heads_path=path, encoder=FakeEncoder())
    scores = screener.score([job, job.model_copy(update={"key": "a:2", "source_id": "2"})])
    assert len(scores) == 2 and all(0 <= s <= 1 for s in scores)
    noul = Screener({"type": "noul", "instructions": "Is this research?"}, heads_path=path, encoder=FakeEncoder())
    assert noul.relevant == ("true",) and noul.version != screener.version
    assert screener.score_key(job) != screener.score_key(job.model_copy(update={"description": "Changed."}))
    assert np.allclose(softmax([[1.0, 2.0, 3.0]]).sum(axis=-1), 1)
    choice = {"type": "choice", "instructions": "Which work?", "criteria": {"research": "Research", "other": "Other"}}
    with pytest.raises(ValueError):Screener(choice, relevant=("missing",), heads_path=path, encoder=FakeEncoder())


def test_tampered_heads_are_rejected(tmp_path):
    path = write_heads(tmp_path)
    meta = json.loads(path.with_suffix(".json").read_text())
    meta["safetensors_sha256"] = "0" * 64
    path.with_suffix(".json").write_text(json.dumps(meta))
    with pytest.raises(ValueError):Heads(path)
