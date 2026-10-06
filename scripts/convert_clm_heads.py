"""One-time conversion of the CLM-v0.1-8B heads pickle to safetensors.

The upstream checkpoint is a torch pickle; it is opened with weights_only=True in a
throwaway environment, so torch never enters the project .venv:

    HF_HOME=~/Developer/ml_source/huggingface .venv/bin/hf download \\
        Contrastive-LM/CLM-v0.1-8B CLM_v0.1-8B.pt --revision e939398d4556fcd9400c76fa8c5a513202f42b0a
    uv run --no-project --with torch --with safetensors python scripts/convert_clm_heads.py \\
        <snapshot>/CLM_v0.1-8B.pt ~/Developer/ml_source/clm/CLM_v0.1-8B.heads.safetensors e939398d4556fcd9400c76fa8c5a513202f42b0a

screen.py refuses heads whose checksum differs from the JSON written here.
"""
import hashlib
import json
import sys
from pathlib import Path

import torch
from safetensors.torch import save_file

# Upstream file verified on 2026-10-06 (Apache-2.0).
KNOWN = {"e939398d4556fcd9400c76fa8c5a513202f42b0a": "b2b4a8c9c2d39263eff78a351eb909a342ce9b3bf21a3f07c1d1bf15f1c4eda5"}

src, out, revision = Path(sys.argv[1]), Path(sys.argv[2]).expanduser(), sys.argv[3]
source_sha256 = hashlib.sha256(src.read_bytes()).hexdigest()
if KNOWN.get(revision, source_sha256) != source_sha256:
    raise SystemExit(f"{src} does not match the recorded sha256 for revision {revision}")
ck = torch.load(src, map_location="cpu", weights_only=True)
tensors = {f"{head}.{name}": t.detach().to(torch.float32).contiguous()
    for head in ("state_head", "action_head") for name, t in ck[head].items()}
cfg = dict(ck["cfg"])
logit_scale = float(torch.as_tensor(ck["logit_scale"]).float())
meta = {"cfg": cfg, "logit_scale": logit_scale, "scale": min(100.0, float(torch.tensor(logit_scale).exp())),
    "projection_dim": ck.get("projection_dim", cfg.get("projection_dim", 512)),
    "source_repo": "Contrastive-LM/CLM-v0.1-8B", "source_file": src.name, "source_revision": revision,
    "source_sha256": source_sha256, "license": "Apache-2.0", "torch_version": torch.__version__}
out.parent.mkdir(parents=True, exist_ok=True)
save_file(tensors, str(out), metadata={"format": "pt", "source_sha256": source_sha256})
meta["safetensors_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
out.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
print(json.dumps(meta, indent=2))
