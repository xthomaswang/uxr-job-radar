"""Optional stage-0 relevance ordering with CLM-v0.1-8B, run on-device with MLX.

CLM (https://huggingface.co/Contrastive-LM/CLM-v0.1-8B, Apache-2.0) is two small
projection heads over frozen Qwen3-8B last-token embeddings. This module reproduces
the upstream text layout (github.com/Contrastive-LM/CLM ``src/clm/schema.py``) and
scoring (``scale * cosine`` softmax over a question's candidates), but replaces the
vLLM bf16 encoder with the locally cached MLX 8-bit Qwen3-8B, so scores can drift
slightly from the reference service.

The score only ORDERS the review queue. It never rejects a posting: every fetched
job still needs the validated Qwen assessment. Weights stay in ~/Developer/ml_source.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np

from .core import Job, digest

ML_SOURCE = Path("~/Developer/ml_source").expanduser()
ENCODER = "mlx-community/Qwen3-8B-8bit"
HEADS_PATH = Path(os.environ.get("UXR_CLM_HEADS", ML_SOURCE / "clm/CLM_v0.1-8B.heads.safetensors")).expanduser()
MAX_STATE_TOKENS = 2048  # the reference encoder serves --max-model-len 2048
MAX_SCALE = 100.0
NOUL_KEYS = ("false", "true")
DESCRIPTION_CHARS = 12000  # pre-trim before tokenizing; tokens are the real bound

# A yes/no question ranked best of four formulations on the small 2026-10-06
# silver-label sample (scripts/clm_eval.py).
QUESTION_VERSION = "uxr-relevance-v2-noul"
QUESTION = {"type": "noul", "instructions": "Is this job mainly user experience research or research-adjacent work on people, customers or products, such as consumer insights, product analytics, behavioral research, human factors or research operations?"}
RELEVANT = ("true",)


# --- upstream CLM text layout (schema.py) ------------------------------------------

def to_text(x, indent=0) -> str:
    if x is None:return ""
    if isinstance(x, str):return x
    if isinstance(x, bool):return "true" if x else "false"
    if isinstance(x, (int, float)):return str(x)
    pad = " " * indent
    if isinstance(x, dict):
        parts = [f"{pad}{k}:\n{to_text(v, indent + 2)}" if isinstance(v, (dict, list)) and v else f"{pad}{k}: {to_text(v)}" for k, v in x.items()]
        return ("\n\n" if indent == 0 else "\n").join(parts)
    if isinstance(x, (list, tuple)):
        return "\n".join(f"{pad}-\n{to_text(v, indent + 2)}" if isinstance(v, (dict, list)) and v else f"{pad}- {to_text(v)}" for v in x)
    return json.dumps(x, ensure_ascii=False)


def state_text(state, instructions) -> str:
    """Context first, question last: the layout the heads were trained on."""
    s, i = to_text(state).strip(), to_text(instructions).strip()
    return f"{s}\n\n{i}" if s and i else (s or i)


def candidates(question: dict) -> tuple[list[str], list[str]]:
    """-> (option keys, candidate texts) exactly as the upstream action head sees them."""
    kind, criteria, ins = question["type"], question.get("criteria"), to_text(question.get("instructions")).strip()
    if kind == "choice":
        if not isinstance(criteria, dict) or not criteria:raise ValueError("choice question needs criteria")
        return list(criteria), [to_text(v) if v not in (None, "") else k for k, v in criteria.items()]
    if kind == "score":
        if not isinstance(criteria, list) or len(criteria) < 2:raise ValueError("score question needs >= 2 levels")
        return [str(i) for i in range(len(criteria))], [to_text(c) for c in criteria]
    if kind != "noul":raise ValueError(f"unknown question type {kind!r}")
    criteria = criteria if isinstance(criteria, dict) else {}
    texts = []
    for k in NOUL_KEYS:
        d = criteria.get(k)
        if d in (None, ""):
            d = (f"Yes. This is true: {ins}" if k == "true" else f"No. This is false: {ins}") if ins else k
        texts.append(f"{k}: {to_text(d)}")
    return list(NOUL_KEYS), texts


def softmax(logits) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64)
    e = np.exp(z - z.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def l2(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-12)


def job_context(job: Job) -> dict:
    """Source facts only; the URL/ID never reach the encoder."""
    return {"Job title": job.title, "Company": job.company, "Location": job.location,
            "Employment type": job.employment_type, "Description": job.description[:DESCRIPTION_CHARS]}


def job_text(job: Job, question: dict = QUESTION) -> str:
    return state_text(job_context(job), question.get("instructions"))


# --- weights -----------------------------------------------------------------------

def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):h.update(chunk)
    return h.hexdigest()


class Heads:
    """State/action MLPs: Linear -> GELU(erf) -> [Linear -> LayerNorm -> GELU]* -> Linear, L2-normalised."""

    def __init__(self, path=HEADS_PATH):
        import mlx.core as mx
        self.path = Path(path)
        self.meta = json.loads(self.path.with_suffix(".json").read_text())
        self.cfg = self.meta["cfg"]
        self.scale = min(MAX_SCALE, math.exp(self.meta["logit_scale"]))
        self.sha256 = sha256_file(self.path)
        if self.sha256 != self.meta.get("safetensors_sha256"):
            raise ValueError("CLM head weights do not match their recorded checksum")
        self.weights = {k: v.astype(mx.float32) for k, v in mx.load(str(self.path)).items()}

    def project(self, which: str, x: np.ndarray) -> np.ndarray:
        if which not in {"state_head", "action_head"}:raise ValueError("Unknown CLM head")
        return head_forward({k[len(which)+1:]: v for k, v in self.weights.items() if k.startswith(which + ".")}, self.cfg, x)


def head_forward(w: dict, cfg: dict, x) -> np.ndarray:
    import mlx.core as mx
    import mlx.nn as nn
    act = {"gelu": nn.gelu, "relu": nn.relu, "silu": nn.silu}[cfg.get("activation", "gelu")]
    # The heads are tiny; the CPU stream keeps exact float32 matmuls (GPU float32
    # matmul measured ~1e-3 relative error on this machine).
    with mx.stream(mx.cpu):
        h = mx.array(np.asarray(x, dtype=np.float32))
        h = act(h @ w["inp.weight"].T + w["inp.bias"])
        for i in range(cfg["depth"] - 2):
            z = h @ w[f"hidden.{i}.weight"].T + w[f"hidden.{i}.bias"]
            if cfg.get("layernorm"):
                z = mx.fast.layer_norm(z, w[f"norms.{i}.weight"], w[f"norms.{i}.bias"], 1e-5)
            z = act(z)
            h = h + z if cfg.get("residual") else z
        out = h @ w["out.weight"].T + w["out.bias"]
        mx.eval(out)
    return l2(np.array(out))


# --- encoder -----------------------------------------------------------------------

class Encoder:
    """Qwen3-8B final-norm hidden state at the last real token (vLLM LAST pooling), L2-normalised."""

    def __init__(self, repo=ENCODER, batch_tokens=16384, max_batch=16):
        self.repo, self.batch_tokens, self.max_batch = repo, batch_tokens, max_batch
        self.model = self.tokenizer = None

    @property
    def revision(self) -> str:
        ref = Path(os.environ.get("HF_HOME", ML_SOURCE / "huggingface")) / "hub" / ("models--" + self.repo.replace("/", "--")) / "refs/main"
        return ref.read_text().strip() if ref.exists() else "unknown"

    def load(self):
        if self.model is None:
            os.environ.setdefault("HF_HOME", str(ML_SOURCE / "huggingface"))
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from mlx_lm import load
            self.model, self.tokenizer = load(self.repo)
        return self

    def encode(self, text: str) -> list[int]:
        # Qwen3 adds no BOS/EOS here, matching the reference /v1/embeddings input.
        return self.load().tokenizer.encode(text)

    def embed_ids(self, sequences: list[list[int]]) -> np.ndarray:
        import mlx.core as mx
        self.load()
        out = np.zeros((len(sequences), self.model.args.hidden_size), np.float32)
        order = sorted(range(len(sequences)), key=lambda i: len(sequences[i]))
        start = 0
        while start < len(order):
            stop = start + 1
            # Lengths ascend, so the last member sets the padded width.
            while stop < len(order) and stop - start < self.max_batch and len(sequences[order[stop]]) * (stop - start + 1) <= self.batch_tokens:
                stop += 1
            batch = order[start:stop]
            width = len(sequences[batch[-1]])
            ids = np.zeros((len(batch), width), np.int32)
            for row, i in enumerate(batch):
                ids[row, :len(sequences[i])] = sequences[i]
            # Right padding is exact under the causal mask: real tokens never attend to it.
            hidden = self.model.model(mx.array(ids))
            last = hidden[mx.arange(len(batch)), mx.array([len(sequences[i]) - 1 for i in batch])].astype(mx.float32)
            mx.eval(last)
            out[batch] = np.array(last)
            start = stop
        return l2(out)

    def close(self):
        import mlx.core as mx
        self.model = self.tokenizer = None
        mx.clear_cache()


def fit_state(encoder: Encoder, context, instructions, max_tokens=MAX_STATE_TOKENS) -> list[int]:
    """Tokenize context + question; trim only the context tail so the question survives."""
    text = state_text(context, instructions)
    ids = encoder.encode(text)
    if len(ids) <= max_tokens:return ids
    body = to_text(context).strip()
    tail = len(encoder.encode("\n\n" + to_text(instructions).strip())) if to_text(instructions).strip() else 0
    budget = max_tokens - tail - 2
    body_ids = encoder.encode(body)
    while budget > 0:
        trimmed = encoder.tokenizer.decode(body_ids[:budget]).rstrip("�")
        ids = encoder.encode(state_text(trimmed, instructions))
        if len(ids) <= max_tokens:return ids
        budget -= len(ids) - max_tokens
    raise ValueError("Question alone exceeds the CLM state budget")


# --- scoring -----------------------------------------------------------------------

class Screener:
    """Queue-ordering relevance score in [0, 1]; lazy model load, explicit close()."""

    def __init__(self, question: dict = QUESTION, relevant=RELEVANT, heads_path=HEADS_PATH,
                 encoder: Encoder | None = None, max_tokens=MAX_STATE_TOKENS, question_version=QUESTION_VERSION):
        self.question, self.relevant, self.max_tokens = question, tuple(relevant), max_tokens
        self.question_version = question_version
        self.keys, self.texts = candidates(question)
        if question["type"] == "noul":self.relevant = ("true",)
        if not set(self.relevant) <= set(self.keys):raise ValueError("Relevant options must be question options")
        self.heads = Heads(heads_path)
        self.encoder = encoder or Encoder()
        self._actions = None

    @property
    def version(self) -> str:
        """Cache-key identity: heads checksum, encoder revision, exact question and budget."""
        return "|".join(["clm-v0.1-8b:" + self.heads.sha256[:16], f"{self.encoder.repo}@{self.encoder.revision[:12]}",
            f"{self.question_version}:{digest([self.question, list(self.relevant)])[:16]}", f"max{self.max_tokens}"])

    def score_key(self, job: Job) -> str:
        return digest([job.content_hash(), self.version])

    def actions(self) -> np.ndarray:
        if self._actions is None:
            raw = self.encoder.embed_ids([self.encoder.encode(t) for t in self.texts])
            self._actions = self.heads.project("action_head", raw)
        return self._actions

    def probabilities(self, states: np.ndarray) -> np.ndarray:
        """[n, hidden] encoder embeddings -> [n, options] distributions."""
        projected = self.heads.project("state_head", states)
        return softmax(self.heads.scale * projected @ self.actions().T)

    def embed_jobs(self, jobs: list[Job]) -> np.ndarray:
        context = self.question.get("instructions")
        return self.encoder.embed_ids([fit_state(self.encoder, job_context(j), context, self.max_tokens) for j in jobs])

    def score(self, jobs: list[Job]) -> list[float]:
        if not jobs:return []
        probs = self.probabilities(self.embed_jobs(jobs))
        columns = [self.keys.index(k) for k in self.relevant]
        return [float(p) for p in probs[:, columns].sum(axis=1)]

    def answer(self, state, question: dict) -> dict[str, float]:
        """Upstream-style single question, for fidelity checks against the model card."""
        keys, texts = candidates(question)
        zs = self.heads.project("state_head", self.encoder.embed_ids([fit_state(self.encoder, state, question.get("instructions"), self.max_tokens)]))
        za = self.heads.project("action_head", self.encoder.embed_ids([self.encoder.encode(t) for t in texts]))
        return dict(zip(keys, softmax(self.heads.scale * zs[0] @ za.T).tolist()))

    def close(self):
        self._actions = None
        self.encoder.close()
