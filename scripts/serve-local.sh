#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
export HF_HOME="${HF_HOME:-$HOME/Developer/ml_source/huggingface}"
export HF_HUB_OFFLINE=1
# A development/evaluation endpoint; bind only to the loopback interface.
# 8013 is the shared local endpoint: if another project already serves it, this
# fails to bind instead of loading a second copy. The defaults match that shared
# server so its other clients see the same behaviour; this client always sends
# an explicit max_tokens.
exec .venv/bin/mlx_lm.server \
  --model "${UXR_MODEL_PATH:-mlx-community/Qwen3.8-27B-8bit}" \
  --host 127.0.0.1 --port "${UXR_LLM_PORT:-8013}" \
  --prompt-cache-bytes 4294967296 --max-tokens 1600 \
  --chat-template-args '{"enable_thinking":false}'
