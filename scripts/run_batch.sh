#!/usr/bin/env bash
# Index Markdown (unchanged files are skipped) and run a question set.
#
#   bash scripts/run_batch.sh                                   # the two sample .md + sample questions
#   bash scripts/run_batch.sh corpus_md/ my_questions.jsonl --concurrency 2
#   SUMMARY=1 STORE=/path/to/store bash scripts/run_batch.sh corpus_md/ q.csv
#
# SUMMARY=1 builds the index with LLM node summaries (slower); default is --no-summary.
# Extra arguments after the question file go to `superindex batch`.
# Output: results/batch/<timestamp>/summary.md and results.jsonl.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "${1:-}" ]]; then MD=("$1"); else MD=(samples/aia_ar2021_excerpt.md samples/di_native_excerpt.md); fi
QUESTIONS="${2:-samples/questions_sample.jsonl}"
shift $(( $# < 2 ? $# : 2 ))

command -v uv >/dev/null 2>&1 || {
    echo "error: uv not found; install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

STORE_ARGS=()
if [[ -n "${STORE:-}" ]]; then STORE_ARGS=(--store "$STORE"); fi
INDEX_ARGS=()
if [[ "${SUMMARY:-0}" != "1" ]]; then INDEX_ARGS=(--no-summary); fi

for md in "${MD[@]}"; do
    echo "== index $md"
    uv run python scripts/si.py index "$md" ${STORE_ARGS[@]+"${STORE_ARGS[@]}"} ${INDEX_ARGS[@]+"${INDEX_ARGS[@]}"}
done

OUT="$ROOT/results/batch/$(date +%Y%m%d-%H%M%S)"
echo "== batch $QUESTIONS"
uv run python scripts/si.py batch "$QUESTIONS" --out "$OUT" ${STORE_ARGS[@]+"${STORE_ARGS[@]}"} "$@"

echo
echo "summary: $OUT/summary.md"
