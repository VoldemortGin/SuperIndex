#!/bin/bash
cd "/Users/yong/WorkBuddy AI/2026-09-20-22-30-52"
while true; do
  d=$(ls results/pageindex_store/docs 2>/dev/null | wc -l | tr -d ' ')
  q=$(grep -c "^\[Q" results/qa_run.log 2>/dev/null | tr -d ' ')
  echo "$(date +%H:%M:%S) indexed=${d}/10 questions_done=${q}"
  if grep -q "Wrote results/qa_results.json" results/qa_run.log 2>/dev/null; then
    echo "=== RUN COMPLETE ==="
    break
  fi
  sleep 60
done
