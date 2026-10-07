#!/usr/bin/env bash
# End-to-end demo of the blind classification review workflow.
# Requires: curl and python3. Usage: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

json_get() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

echo "== 1. create batch (credentials appear only in this response) =="
CREATE=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "allowed_categories": ["cat", "dog", "bird"],
  "samples": [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"}
  ]
}')
echo "$CREATE" | python3 -m json.tool
BID=$(json_get "d['batch_id']" <<<"$CREATE")
R1=$(json_get "d['reviewer1_token']" <<<"$CREATE")
R2=$(json_get "d['reviewer2_token']" <<<"$CREATE")
ARB=$(json_get "d['arbiter_token']" <<<"$CREATE")

echo "== 2. public view: only codes and phase =="
curl -sS "$BASE/batches/$BID" | python3 -m json.tool

echo "== 3. reviewer 1 submits =="
curl -sS -X POST "$BASE/batches/$BID/submissions" \
  -H "X-Reviewer-Token: $R1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}' | python3 -m json.tool

echo "== 4. reviewer 2 submits (disagrees on S03) -> adjudicating =="
curl -sS -X POST "$BASE/batches/$BID/submissions" \
  -H "X-Reviewer-Token: $R2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}' | python3 -m json.tool

echo "== 5. arbiter lists disagreements =="
curl -sS "$BASE/batches/$BID/disagreements" -H "X-Arbiter-Token: $ARB" | python3 -m json.tool

echo "== 6. arbiter adjudicates -> sealed =="
curl -sS -X POST "$BASE/batches/$BID/adjudications" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}' | python3 -m json.tool

echo "== 7. unblinded results =="
curl -sS "$BASE/batches/$BID/results" | python3 -m json.tool

echo "== 7b. reviewer replacement needs an unsealed batch: open a fresh one =="
BATCHB=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "allowed_categories": ["cat", "dog", "bird"],
  "samples": [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"}
  ]
}')
BIDB=$(json_get "d['batch_id']" <<<"$BATCHB")
ARBB=$(json_get "d['arbiter_token']" <<<"$BATCHB")
R1B_OLD=$(json_get "d['reviewer1_token']" <<<"$BATCHB")
R2B_OLD=$(json_get "d['reviewer2_token']" <<<"$BATCHB")

echo "== 7c. both reviewers submit (disagreement on S03) -> adjudicating =="
curl -sS -X POST "$BASE/batches/$BIDB/submissions" \
  -H "X-Reviewer-Token: $R1B_OLD" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}' | python3 -m json.tool
curl -sS -X POST "$BASE/batches/$BIDB/submissions" \
  -H "X-Reviewer-Token: $R2B_OLD" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}' | python3 -m json.tool

echo "== 7d. arbiter rotates reviewer 2's credential: seat 2 submission voided, back to collecting =="
REPLACED=$(curl -sS -X POST "$BASE/batches/$BIDB/reviewer-replacements" \
  -H "X-Arbiter-Token: $ARBB" -H 'Content-Type: application/json' \
  -d '{"reviewer": 2}')
echo "$REPLACED" | python3 -m json.tool
R2B_NEW=$(json_get "d['reviewer_token']" <<<"$REPLACED")

echo "== 7e. old reviewer 2 credential is dead (submit and query both 401) =="
curl -sS -o /dev/null -w 'old token submit -> %{http_code}\n' -X POST \
  "$BASE/batches/$BIDB/submissions" \
  -H "X-Reviewer-Token: $R2B_OLD" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}'
curl -sS -o /dev/null -w 'old token query  -> %{http_code}\n' \
  "$BASE/batches/$BIDB/submission" -H "X-Reviewer-Token: $R2B_OLD"

echo "== 7f. old disagreements can no longer be adjudicated; reviewer 1 submission survives =="
curl -sS -o /dev/null -w 'disagreements -> %{http_code}\n' \
  "$BASE/batches/$BIDB/disagreements" -H "X-Arbiter-Token: $ARBB"
curl -sS "$BASE/batches/$BIDB/submission" -H "X-Reviewer-Token: $R1B_OLD" | python3 -m json.tool

echo "== 7g. successor independently resubmits -> adjudicating, then seals =="
curl -sS -X POST "$BASE/batches/$BIDB/submissions" \
  -H "X-Reviewer-Token: $R2B_NEW" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}' | python3 -m json.tool
curl -sS -X POST "$BASE/batches/$BIDB/adjudications" \
  -H "X-Arbiter-Token: $ARBB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}' | python3 -m json.tool

echo "== 7h. a sealed batch refuses replacement (409) =="
curl -sS -o /dev/null -w 'sealed replacement -> %{http_code}\n' -X POST \
  "$BASE/batches/$BIDB/reviewer-replacements" \
  -H "X-Arbiter-Token: $ARBB" -H 'Content-Type: application/json' \
  -d '{"reviewer": 1}'

echo "== 8. errata: S02 truth corrected dog -> cat (expected_revision 0) =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S02", "true_label": "cat"}]}' | python3 -m json.tool

echo "== 9. stale expected_revision is rejected with the current revision =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S03", "true_label": "dog"}]}' | python3 -m json.tool

echo "== 10. version chain =="
curl -sS "$BASE/batches/$BID/errata" | python3 -m json.tool

echo "== 11. versioned view: revision 1 recomputes correctness against the new truth =="
curl -sS "$BASE/batches/$BID/errata/1" | python3 -m json.tool

echo "== 12. plain results still return the sealed original (revision 0) =="
curl -sS "$BASE/batches/$BID/results" | python3 -m json.tool

echo "== 13. arbiter opens an independent re-evaluation on truth revision 1 =="
RECREATE=$(curl -sS -X POST "$BASE/batches/$BID/reevaluations" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"truth_revision": 1}')
echo "$RECREATE" | python3 -m json.tool
NBID=$(json_get "d['batch_id']" <<<"$RECREATE")
NR1=$(json_get "d['reviewer1_token']" <<<"$RECREATE")
NR2=$(json_get "d['reviewer2_token']" <<<"$RECREATE")
NARB=$(json_get "d['arbiter_token']" <<<"$RECREATE")

echo "== 14. new round is blind: codes only; comparison is closed before sealing =="
curl -sS "$BASE/batches/$NBID" | python3 -m json.tool
curl -sS "$BASE/batches/$NBID/comparison" | python3 -m json.tool || true

echo "== 15. two new reviewers submit (they disagree on S03) =="
curl -sS -X POST "$BASE/batches/$NBID/submissions" \
  -H "X-Reviewer-Token: $NR1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}' | python3 -m json.tool
curl -sS -X POST "$BASE/batches/$NBID/submissions" \
  -H "X-Reviewer-Token: $NR2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}' | python3 -m json.tool

echo "== 16. new arbiter adjudicates S03 -> cat, sealing the re-evaluation =="
curl -sS -X POST "$BASE/batches/$NBID/adjudications" \
  -H "X-Arbiter-Token: $NARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "cat"}}' | python3 -m json.tool

echo "== 17. cross-round comparison (truth = chosen revision 1; source labels from the sealed original) =="
curl -sS "$BASE/batches/$NBID/comparison" | python3 -m json.tool

echo "== 18. a later source erratum (revision 2) never changes the snapshot or the comparison =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "corrections": [{"code": "S03", "true_label": "cat"}]}' | python3 -m json.tool
curl -sS "$BASE/batches/$NBID/comparison" | python3 -m json.tool
