#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PYTHONPATH="src:.:${PYTHONPATH:-}"
runner_python="${BIRD_PYTHON:-/tmp/sql-agent-acceptance-venv/bin/python}"
result_root="${BIRD_OUTPUT:-outputs/validation/bird-agent-small-models}"
# Fixed reviewer: only the Planner model changes in this comparison.
ollama list >/dev/null
for model in qwen2.5-coder:7b qwen3:4b gemma3:4b qwen3:0.6b; do
  if ! ollama show "$model" >/dev/null 2>&1; then
    ollama pull "$model"
  fi
done
for model in qwen3:4b gemma3:4b qwen3:0.6b; do
  result_dir="$result_root/${model//:/-}"
  resume_args=()
  if [[ -f "$result_dir/manifest.json" ]]; then resume_args=(--resume); fi
  thinking_args=()
  if [[ "$model" == qwen3:* ]]; then thinking_args=(--disable-thinking); fi
  "$runner_python" -m scripts.evaluation.evaluate_bird_agent \
    --data "${BIRD_DATA:-data/local/bird_mini_dev}" \
    --profile generic --variant full_no_rag --retrieval bm25 \
    --model "$model" --reviewer qwen2.5-coder:7b --output "$result_dir" \
    "${thinking_args[@]}" "${resume_args[@]}"
done
"$runner_python" -m scripts.evaluation.summarize_bird_agent_small_models "$result_root"
