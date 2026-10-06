#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
export HF_HOME="${HF_HOME:-$HOME/Developer/ml_source/huggingface}"
export HF_HUB_OFFLINE=1
# A development/evaluation endpoint; bind only to the loopback interface.
exec .venv/bin/mlx_lm.server \
  --model "${UXR_MODEL_PATH:-mlx-community/Qwen3.8-27B-8bit}" \
  --host 127.0.0.1 --port "${UXR_LLM_PORT:-8012}" \
  --prompt-cache-bytes 4294967296 --max-tokens 900 \
  --chat-template-args '{"enable_thinking":false}'
