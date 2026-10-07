import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    ReplacementRequest,
    create_app,
    logic_replace_reviewer,
    logic_submit,
)

SAMPLES = [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"},
]
CATEGORIES = ["cat", "dog", "bird"]

# reviewer1 gets everything right; reviewer2 flips S03
R1_LABELS = {"S01": "cat", "S02": "dog", "S03": "bird"}
R2_LABELS = {"S01": "cat", "S02": "dog", "S03": "cat"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_batch(client):
    r = client.post("/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES})
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, batch_id, token, labels):
    return client.post(
        f"/batches/{batch_id}/submissions",
        json={"labels": labels},
        headers={"X-Reviewer-Token": token},
    )


def replace(client, batch_id, token, reviewer):
    headers = {"X-Arbiter-Token": token} if token is not None else {}
    return client.post(
        f"/batches/{batch_id}/reviewer-replacements",
        json={"reviewer": reviewer},
        headers=headers,
    )


def drive_to_adjudicating(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 201
    assert submit(client, bid, batch["reviewer2_token"], R2_LABELS).status_code == 201
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"
    return batch


# --------------------------------------------------------------------------
# Basic replacement semantics
# --------------------------------------------------------------------------

def test_replace_unsubmitted_seat_rotates_credential(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    old_r1 = batch["reviewer1_token"]

    r = replace(client, bid, batch["arbiter_token"], 1)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reviewer"] == 1
    assert body["phase"] == "collecting"
    assert body["voided_submission"] is False
    assert body["other_reviewer_submitted"] is False
    new_token = body["replacement_token"]
    assert new_token and new_token != old_r1
    assert new_token not in (batch["reviewer2_token"], batch["arbiter_token"])
    # only the new credential is shown; nothing else leaks
    assert "true_label" not in r.text

    # old credential is dead: cannot query the seat, cannot submit
    assert client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": old_r1}
    ).status_code == 401
    assert submit(client, bid, old_r1, R1_LABELS).status_code == 401

    # new credential works
    assert submit(client, bid, new_token, R1_LABELS).status_code == 201


def test_replace_after_other_reviewer_submitted_keeps_other_submission(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)

    r = replace(client, bid, batch["arbiter_token"], 2)
    assert r.status_code == 201
    body = r.json()
    assert body["voided_submission"] is False
    assert body["other_reviewer_submitted"] is True

    # seat 1's submission is untouched and still visible to its holder
    own = client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]}
    ).json()
    assert own["submitted"] is True and own["labels"] == R1_LABELS

    # old seat-2 credential is dead; successor can submit fresh
    assert submit(client, bid, batch["reviewer2_token"], R2_LABELS).status_code == 401
    assert submit(client, bid, body["replacement_token"], R2_LABELS).status_code == 201
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"


def test_replace_submitted_seat_in_collecting_voids_only_that_submission(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)

    r = replace(client, bid, batch["arbiter_token"], 1)
    assert r.status_code == 201
    body = r.json()
    assert body["voided_submission"] is True
    assert body["other_reviewer_submitted"] is False
    assert body["phase"] == "collecting"

    # the voided submission is gone; the old token cannot query or resubmit
    assert client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]}
    ).status_code == 401
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 401

    # successor submits independently; nothing from the voided labels lingers
    assert submit(client, bid, body["replacement_token"], R1_LABELS).status_code == 201
    assert client.get(f"/batches/{bid}/results").status_code == 409  # still one review short


def test_replace_in_adjudicating_voids_submission_and_old_disagreement(client):
    batch = drive_to_adjudicating(client)
    bid = batch["batch_id"]

    r = replace(client, bid, batch["arbiter_token"], 2)
    assert r.status_code == 201
    body = r.json()
    assert body["voided_submission"] is True
    assert body["other_reviewer_submitted"] is True
    assert body["phase"] == "collecting"

    # batch is back to collecting; arbiter can no longer rule the old dispute
    assert client.get(f"/batches/{bid}").json()["phase"] == "collecting"
    r = client.get(
        f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]}
    )
    assert r.status_code == 409
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 409

    # old seat-2 token is dead for both query and submission
    assert client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer2_token"]}
    ).status_code == 401
    assert submit(client, bid, batch["reviewer2_token"], R2_LABELS).status_code == 401

    # seat 1's submission is preserved
    own = client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]}
    ).json()
    assert own["submitted"] is True and own["labels"] == R1_LABELS

    # successor submits independently and the normal flow restarts
    assert submit(client, bid, body["replacement_token"], R2_LABELS).status_code == 201
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"
    disagreements = client.get(
        f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]}
    ).json()["disagreements"]
    assert disagreements == [
        {"code": "S03", "reviewer1_label": "bird", "reviewer2_label": "cat"}
    ]
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "cat"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    results = client.get(f"/batches/{bid}/results").json()
    by_code = {row["code"]: row for row in results["results"]}
    assert by_code["S03"]["final_label"] == "cat"  # new successor's choice prevailed


def test_repeated_replacement_chains_tokens(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    first = batch["reviewer1_token"]

    body1 = replace(client, bid, batch["arbiter_token"], 1).json()
    second = body1["replacement_token"]
    body2 = replace(client, bid, batch["arbiter_token"], 1).json()
    third = body2["replacement_token"]
    assert len({first, second, third}) == 3

    # every previous generation is invalid
    assert submit(client, bid, first, R1_LABELS).status_code == 401
    assert submit(client, bid, second, R1_LABELS).status_code == 401
    assert submit(client, bid, third, R1_LABELS).status_code == 201


# --------------------------------------------------------------------------
# Guards: sealed batch, auth, validation, not found
# --------------------------------------------------------------------------

def test_replace_rejected_on_sealed_batch_and_leaves_nothing(client):
    batch = drive_to_adjudicating(client)
    bid = batch["batch_id"]
    client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    before = client.get(f"/batches/{bid}/results").json()

    r = replace(client, bid, batch["arbiter_token"], 1)
    assert r.status_code == 409
    # no credential leaked in the error
    assert "token" not in r.text.lower() or "arbiter" in r.text.lower()

    # sealed results unchanged and original credentials still valid
    assert client.get(f"/batches/{bid}/results").json() == before
    own = client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]}
    )
    assert own.status_code == 200 and own.json()["submitted"] is True


def test_replace_requires_arbiter_token(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    # missing
    assert replace(client, bid, None, 1).status_code == 401
    # bogus
    assert replace(client, bid, "nope", 1).status_code == 401
    # reviewer credentials cannot replace seats
    assert replace(client, bid, batch["reviewer1_token"], 2).status_code == 401
    assert replace(client, bid, batch["reviewer2_token"], 1).status_code == 401
    # failed attempts changed no credentials
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 201
    assert submit(client, bid, batch["reviewer2_token"], R2_LABELS).status_code == 201


def test_replace_validation_and_not_found(client):
    batch = make_batch(client)
    assert replace(client, "does-not-exist", batch["arbiter_token"], 1).status_code == 404

    r = client.post(
        f"/batches/{batch['batch_id']}/reviewer-replacements",
        json={"reviewer": 3},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 422
    r = client.post(
        f"/batches/{batch['batch_id']}/reviewer-replacements",
        json={"reviewer": 0},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 422
    # rejection changed nothing: original credentials still work
    assert submit(client, batch["batch_id"], batch["reviewer1_token"], R1_LABELS).status_code == 201


# --------------------------------------------------------------------------
# Blindness: the successor starts as blind as any other reviewer
# --------------------------------------------------------------------------

def test_successor_remains_blind(client):
    batch = drive_to_adjudicating(client)
    bid = batch["batch_id"]
    body = replace(client, bid, batch["arbiter_token"], 2).json()
    new_token = body["replacement_token"]

    # own-submission query shows nothing submitted and never the other labels/truth
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": new_token})
    assert r.status_code == 200
    assert r.json() == {"batch_id": bid, "reviewer": 2, "submitted": False}

    # public view stays blind
    public = client.get(f"/batches/{bid}").json()
    assert set(public) == {"batch_id", "phase", "samples"}
    assert public["phase"] == "collecting"
    assert client.get(f"/batches/{bid}/results").status_code == 409

    # after submitting, still only own labels
    submit(client, bid, new_token, R2_LABELS)
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": new_token})
    own = r.json()
    assert own["submitted"] is True and own["labels"] == R2_LABELS
    assert "true_label" not in r.text


# --------------------------------------------------------------------------
# Transaction ordering / atomicity
# --------------------------------------------------------------------------

def test_concurrent_replacement_and_old_credential_submission(tmp_path):
    """A replacement serialized against an old-token submission: either the
    submission wins (409 on replacement attempt ordering aside) or the
    replacement wins (submission 401); the voided seat never holds both."""
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = make_batch(client)
        bid = batch["batch_id"]
        old_token = batch["reviewer1_token"]
        arb = batch["arbiter_token"]
        submit(client, bid, batch["reviewer2_token"], R2_LABELS)

        repl_outcomes = []
        sub_outcomes = []
        start = threading.Event()

        def replace_worker():
            start.wait()
            try:
                res = logic_replace_reviewer(store, bid, arb, ReplacementRequest(reviewer=1))
                repl_outcomes.append(("ok", res["replacement_token"]))
            except HTTPException as exc:
                repl_outcomes.append((exc.status_code, None))

        def submit_worker():
            start.wait()
            try:
                logic_submit(store, bid, old_token, R1_LABELS)
                sub_outcomes.append("ok")
            except HTTPException as exc:
                sub_outcomes.append(exc.status_code)

        threads = [threading.Thread(target=replace_worker), threading.Thread(target=submit_worker)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

        assert len(sub_outcomes) == 1 and len(repl_outcomes) == 1
        with store.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM submissions WHERE batch_id = ? AND reviewer = 1",
                (bid,),
            ).fetchone()["c"]

        # exactly one submission (seat 2) survives in either ordering
        with store.read() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM submissions WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
        assert total == 1

        if repl_outcomes[0][0] == "ok" and sub_outcomes[0] == 401:
            # replacement committed first: old token rejected, seat empty;
            # the fresh token is usable
            assert n == 0
            assert submit(client, bid, repl_outcomes[0][1], R1_LABELS).status_code == 201
        elif sub_outcomes[0] == "ok":
            # submission committed first; the (still-unsealed) replacement
            # then succeeded and voided it
            assert repl_outcomes[0][0] == "ok"
            assert n == 0
        else:  # pragma: no cover - the two orderings above are exhaustive
            raise AssertionError((repl_outcomes, sub_outcomes))


def test_concurrent_replacements_only_last_token_works(tmp_path):
    app = create_app(str(tmp_path / "race2.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = make_batch(client)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        tokens = []
        errors = []
        start = threading.Event()

        def worker():
            start.wait()
            try:
                res = logic_replace_reviewer(store, bid, arb, ReplacementRequest(reviewer=1))
                tokens.append(res["replacement_token"])
            except HTTPException as exc:
                errors.append(exc.status_code)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

        assert not errors
        assert len(tokens) == 6
        assert len(set(tokens)) == 6
        # every token but the last-issued one is invalid
        for tok in tokens[:-1]:
            assert submit(client, bid, tok, R1_LABELS).status_code == 401
        assert submit(client, bid, tokens[-1], R1_LABELS).status_code == 201


def test_failed_replacement_is_atomic(tmp_path):
    """A 409 against a sealed batch leaves no new credential and no deleted
    rows; a bad-reviewer request likewise leaves the state untouched."""
    db = str(tmp_path / "atomic.db")
    app = create_app(db)
    store = app.state.store
    with TestClient(app) as client:
        batch = drive_to_adjudicating(client)
        bid = batch["batch_id"]
        client.post(
            f"/batches/{bid}/adjudications",
            json={"decisions": {"S03": "bird"}},
            headers={"X-Arbiter-Token": batch["arbiter_token"]},
        )
        with store.read() as conn:
            before_subs = conn.execute(
                "SELECT reviewer FROM submissions WHERE batch_id = ? ORDER BY reviewer", (bid,)
            ).fetchall()
            assert [r["reviewer"] for r in before_subs] == [1, 2]

        assert replace(client, bid, batch["arbiter_token"], 1).status_code == 409
        assert replace(client, bid, batch["arbiter_token"], 2).status_code == 409

        with store.read() as conn:
            after = [
                r["reviewer"]
                for r in conn.execute(
                    "SELECT reviewer FROM submissions WHERE batch_id = ? ORDER BY reviewer",
                    (bid,),
                ).fetchall()
            ]
            finals = conn.execute(
                "SELECT COUNT(*) AS c FROM finals WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
        assert after == [1, 2]
        assert finals == 3


# --------------------------------------------------------------------------
# Re-evaluation batches and persistence
# --------------------------------------------------------------------------

def test_replace_works_on_unsealed_reevaluation_batch(client):
    batch = drive_to_adjudicating(client)
    bid = batch["batch_id"]
    client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    reeval = client.post(
        f"/batches/{bid}/reevaluations",
        json={"truth_revision": 0},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    ).json()
    nbid = reeval["batch_id"]
    submit(client, nbid, reeval["reviewer1_token"], R1_LABELS)

    r = replace(client, nbid, reeval["arbiter_token"], 2)
    assert r.status_code == 201
    new_token = r.json()["replacement_token"]
    # re-evaluation stays blind: comparison still closed, no source leakage
    assert client.get(f"/batches/{nbid}/comparison").status_code == 409
    assert submit(client, nbid, new_token, R2_LABELS).status_code == 201
    client.post(
        f"/batches/{nbid}/adjudications",
        json={"decisions": {"S03": "cat"}},
        headers={"X-Arbiter-Token": reeval["arbiter_token"]},
    )
    assert client.get(f"/batches/{nbid}").json()["phase"] == "sealed"
    # once sealed, replacement is refused on the re-evaluation too
    assert replace(client, nbid, reeval["arbiter_token"], 1).status_code == 409


def test_replacement_survives_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    app1 = create_app(db)
    with TestClient(app1) as client1:
        batch = make_batch(client1)
        bid = batch["batch_id"]
        submit(client1, bid, batch["reviewer1_token"], R1_LABELS)
        body = replace(client1, bid, batch["arbiter_token"], 2).json()
        new_token = body["replacement_token"]
        assert client1.get(f"/batches/{bid}").json()["phase"] == "collecting"

    app2 = create_app(db)
    with TestClient(app2) as client2:
        # old token still dead, new token still alive, seat 1 submission kept
        assert submit(client2, bid, batch["reviewer2_token"], R2_LABELS).status_code == 401
        assert submit(client2, bid, new_token, R2_LABELS).status_code == 201
        own = client2.get(
            f"/batches/{bid}/submission",
            headers={"X-Reviewer-Token": batch["reviewer1_token"]},
        ).json()
        assert own["submitted"] is True
        assert client2.get(f"/batches/{bid}").json()["phase"] == "adjudicating"
