"""Tests for pre-seal reviewer credential replacement (seat succession)."""

import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    ReviewerReplacementRequest,
    create_app,
    logic_adjudicate,
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
# agreeing classification -> second submission seals immediately
AGREE_LABELS = dict(R1_LABELS)


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_batch(client):
    r = client.post(
        "/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES}
    )
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


def adjudicate(client, batch_id, token, decisions):
    return client.post(
        f"/batches/{batch_id}/adjudications",
        json={"decisions": decisions},
        headers={"X-Arbiter-Token": token},
    )


# --------------------------------------------------------------------------
# Basic rotation: no submission yet on the seat
# --------------------------------------------------------------------------

def test_replace_empty_seat_issues_new_credential(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    r = replace(client, bid, batch["arbiter_token"], 2)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {
        "batch_id": bid,
        "reviewer": 2,
        "reviewer_token": body["reviewer_token"],
        "phase": "collecting",
        "voided_submission": False,
        "notice": body["notice"],
    }
    new_token = body["reviewer_token"]
    assert new_token and new_token not in (
        batch["reviewer1_token"], batch["reviewer2_token"], batch["arbiter_token"]
    )

    # the old credential of that seat is dead immediately
    assert submit(client, bid, batch["reviewer2_token"], R2_LABELS).status_code == 401
    assert (
        client.get(
            f"/batches/{bid}/submission",
            headers={"X-Reviewer-Token": batch["reviewer2_token"]},
        ).status_code
        == 401
    )
    # the other seat and the arbiter credential are untouched
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 201
    assert (
        client.get(
            f"/batches/{bid}/disagreements",
            headers={"X-Arbiter-Token": batch["arbiter_token"]},
        ).status_code
        == 409
    )

    # the successor holds the seat and can submit as reviewer 2
    r = client.get(
        f"/batches/{bid}/submission",
        headers={"X-Reviewer-Token": new_token},
    )
    assert r.status_code == 200 and r.json()["reviewer"] == 2
    assert r.json()["submitted"] is False
    r = submit(client, bid, new_token, R2_LABELS)
    assert r.status_code == 201 and r.json()["reviewer"] == 2
    assert r.json()["phase"] == "adjudicating"


def test_replace_is_allowed_while_collecting_even_without_other_submission(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    # neither reviewer has submitted yet; rotating seat 1 is still valid
    r = replace(client, bid, batch["arbiter_token"], 1)
    assert r.status_code == 201
    assert r.json()["voided_submission"] is False
    old = batch["reviewer1_token"]
    new = r.json()["reviewer_token"]
    assert submit(client, bid, old, R1_LABELS).status_code == 401
    assert submit(client, bid, new, R1_LABELS).status_code == 201


def test_seat_can_be_rotated_more_than_once(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    t1 = replace(client, bid, arb, 1).json()["reviewer_token"]
    t2 = replace(client, bid, arb, 1).json()["reviewer_token"]
    assert t1 != t2 != batch["reviewer1_token"]
    assert submit(client, bid, t1, R1_LABELS).status_code == 401
    assert submit(client, bid, t2, R1_LABELS).status_code == 201


# --------------------------------------------------------------------------
# Rotating a seat that already submitted: its classification is voided, the
# batch returns to collecting and the other submission survives
# --------------------------------------------------------------------------

def test_replace_voids_collecting_submission_and_keeps_other_seat(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)

    r = replace(client, bid, arb, 1)
    assert r.status_code == 201
    body = r.json()
    assert body["voided_submission"] is True
    assert body["phase"] == "collecting"
    successor = body["reviewer_token"]

    # batch is back to collecting with zero submissions on seat 1 ...
    assert client.get(f"/batches/{bid}").json()["phase"] == "collecting"
    own = client.get(
        f"/batches/{bid}/submission",
        headers={"X-Reviewer-Token": successor},
    ).json()
    assert own == {"batch_id": bid, "reviewer": 1, "submitted": False}

    # ... while seat 2 was never touched
    assert submit(client, bid, batch["reviewer2_token"], R2_LABELS).status_code == 201
    # successor now submits fresh, independently; disagreement recomputed
    r = submit(client, bid, successor, R1_LABELS)
    assert r.json()["phase"] == "adjudicating"


def test_replace_during_adjudicating_erases_disagreement_state(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"

    # reviewer 2 drops out; arbiter rotates seat 2
    r = replace(client, bid, arb, 2)
    assert r.status_code == 201
    assert r.json()["voided_submission"] is True
    assert r.json()["phase"] == "collecting"
    successor = r.json()["reviewer_token"]

    # the old disagreements can no longer be adjudicated
    assert (
        client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": arb}).status_code
        == 409
    )
    assert adjudicate(client, bid, arb, {"S03": "bird"}).status_code == 409

    # the old credential is dead
    assert (
        client.get(
            f"/batches/{bid}/submission",
            headers={"X-Reviewer-Token": batch["reviewer2_token"]},
        ).status_code
        == 401
    )

    # only one submission (reviewer 1) remains
    with client.app.state.store.read() as conn:
        rows = conn.execute(
            "SELECT reviewer FROM submissions WHERE batch_id = ? ORDER BY reviewer", (bid,)
        ).fetchall()
        assert [row["reviewer"] for row in rows] == [1]
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM adjudications WHERE batch_id = ?", (bid,)
        ).fetchone()["c"] == 0

    # successor independently submits the same labels; disagreement is computed anew
    r = submit(client, bid, successor, R2_LABELS)
    assert r.status_code == 201 and r.json()["phase"] == "adjudicating"
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": arb})
    assert r.status_code == 200
    assert [d["code"] for d in r.json()["disagreements"]] == ["S03"]
    r = adjudicate(client, bid, arb, {"S03": "bird"})
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    body = client.get(f"/batches/{bid}/results").json()
    assert body["summary"]["reviewer1_correct"] == 3
    assert body["summary"]["reviewer2_correct"] == 2


def test_replace_seat1_during_adjudicating_keeps_reviewer2(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)

    r = replace(client, bid, arb, 1)
    assert r.status_code == 201 and r.json()["voided_submission"] is True
    successor = r.json()["reviewer_token"]

    # reviewer 2's classification survived intact
    own = client.get(
        f"/batches/{bid}/submission",
        headers={"X-Reviewer-Token": batch["reviewer2_token"]},
    ).json()
    assert own["submitted"] is True and own["labels"] == R2_LABELS

    # seat 1 successor re-submits agreeing with reviewer 2 -> second
    # submission present again, they agree on everything -> immediate seal
    r = submit(client, bid, successor, R2_LABELS)
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    body = client.get(f"/batches/{bid}/results").json()
    assert body["summary"]["disagreement_count"] == 0
    assert body["summary"]["reviewer1_correct"] == 2  # successor's labels


def test_agreement_round_trip_through_replacement(client):
    """Void then resubmit with agreement: normal auto-seal flow resumes."""
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)  # adjudicating

    successor = replace(client, bid, arb, 2).json()["reviewer_token"]
    r = submit(client, bid, successor, AGREE_LABELS)  # now both agree
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    body = client.get(f"/batches/{bid}/results").json()
    assert all(row["final_label"] == row["true_label"] for row in body["results"])


# --------------------------------------------------------------------------
# Sealed batches never allow replacement
# --------------------------------------------------------------------------

def test_sealed_batch_refuses_replacement(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], AGREE_LABELS)  # auto-seal
    assert client.get(f"/batches/{bid}").json()["phase"] == "sealed"

    old_hashes_before = _token_hashes(client, bid)
    r = replace(client, bid, arb, 1)
    assert r.status_code == 409
    r = replace(client, bid, arb, 2)
    assert r.status_code == 409
    # nothing changed: the sealed results and credential hashes are intact
    assert _token_hashes(client, bid) == old_hashes_before
    assert client.get(f"/batches/{bid}/results").status_code == 200


def _token_hashes(client, bid):
    with client.app.state.store.read() as conn:
        row = conn.execute(
            "SELECT reviewer1_token_hash, reviewer2_token_hash, arbiter_token_hash"
            " FROM batches WHERE id = ?",
            (bid,),
        ).fetchone()
        return tuple(row)


# --------------------------------------------------------------------------
# Authorization / validation / atomicity
# --------------------------------------------------------------------------

def test_replace_requires_arbiter_credential(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    for headers in (
        {},
        {"X-Arbiter-Token": "nope"},
        {"X-Arbiter-Token": batch["reviewer1_token"]},
        {"X-Arbiter-Token": batch["reviewer2_token"]},
    ):
        r = client.post(
            f"/batches/{bid}/reviewer-replacements",
            json={"reviewer": 1},
            headers=headers,
        )
        assert r.status_code == 401, headers


def test_replace_validates_reviewer_number(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    before = _token_hashes(client, bid)
    for reviewer in (0, 3, -1):
        r = replace(client, bid, arb, reviewer)
        assert r.status_code == 422, reviewer
    # malformed / missing reviewer field is a request-body validation error
    r = client.post(
        f"/batches/{bid}/reviewer-replacements",
        json={"reviewer": "one"},
        headers={"X-Arbiter-Token": arb},
    )
    assert r.status_code == 422
    r = client.post(
        f"/batches/{bid}/reviewer-replacements",
        json={},
        headers={"X-Arbiter-Token": arb},
    )
    assert r.status_code == 422
    # failed validation changed nothing
    assert _token_hashes(client, bid) == before
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 201


def test_replace_missing_batch_is_404(client):
    assert (
        client.post(
            "/batches/does-not-exist/reviewer-replacements",
            json={"reviewer": 1},
            headers={"X-Arbiter-Token": "x"},
        ).status_code
        == 404
    )


def test_failed_replacement_leaves_no_new_credential_or_partial_void(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)  # adjudicating
    hashes_before = _token_hashes(client, bid)

    # invalid reviewer: the whole request rolls back
    r = replace(client, bid, arb, 3)
    assert r.status_code == 422
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"
    assert _token_hashes(client, bid) == hashes_before
    # both original submissions still present and queryable
    for token, labels in ((batch["reviewer1_token"], R1_LABELS),
                          (batch["reviewer2_token"], R2_LABELS)):
        own = client.get(
            f"/batches/{bid}/submission", headers={"X-Reviewer-Token": token}
        ).json()
        assert own["submitted"] is True and own["labels"] == labels

    # wrong/missing arbiter credential likewise changes nothing
    assert replace(client, bid, "nope", 1).status_code == 401
    assert replace(client, bid, None, 1).status_code == 401
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"
    assert _token_hashes(client, bid) == hashes_before


# --------------------------------------------------------------------------
# Blindness around replacement
# --------------------------------------------------------------------------

def test_blindness_preserved_before_and_after_replacement(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)

    successor = replace(client, bid, arb, 2).json()["reviewer_token"]
    submit(client, bid, successor, R2_LABELS)  # adjudicating again

    # public query stays blind: only codes and phase, no labels/truth/tokens
    r = client.get(f"/batches/{bid}")
    assert r.json() == {
        "batch_id": bid,
        "phase": "adjudicating",
        "samples": ["S01", "S02", "S03"],
    }
    assert "true_label" not in r.text and "token" not in r.text

    # the successor cannot read the other reviewer's classification or truth
    own = client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": successor}
    )
    assert own.status_code == 200
    assert own.json()["submitted"] is True
    assert own.json()["labels"] == R2_LABELS
    assert "true_label" not in own.text

    # results stay closed until sealed
    assert client.get(f"/batches/{bid}/results").status_code == 409
    # replacement response exposes only the new seat credential, no other secret
    r = replace(client, bid, arb, 2)
    assert r.status_code == 201
    text = r.text
    assert batch["reviewer1_token"] not in text
    assert batch["arbiter_token"] not in text


# --------------------------------------------------------------------------
# Interleaving: replacements against submissions / seals / adjudications
# take effect in transaction order
# --------------------------------------------------------------------------

def test_interleaved_replacement_and_agreeing_second_submission(tmp_path):
    """One replacement races the second (agreeing) submission.

    If the submission commits first, the batch auto-seals and the replacement
    is rejected 409; the old credential keeps working on the sealed batch
    (submissions closed) and no new credential was issued. If the replacement
    commits first, the first reviewer's submission is the only one left and
    the batch is collecting; the second reviewer's old token is dead while
    the returned new token can submit. Repeated runs must always land on one
    of these two linearizable states, never a mix.
    """
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = make_batch(client)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        r1, r2 = batch["reviewer1_token"], batch["reviewer2_token"]
        assert submit(client, bid, r1, R1_LABELS).status_code == 201

        replacement_result = {}

        def replace_worker():
            try:
                out = logic_replace_reviewer(
                    store, bid, arb, ReviewerReplacementRequest(reviewer=2)
                )
                replacement_result["ok"] = out["reviewer_token"]
            except HTTPException as exc:
                replacement_result["error"] = exc.status_code

        def submit_worker():
            try:
                logic_submit(store, bid, r2, AGREE_LABELS)
                replacement_result["submit"] = 201
            except HTTPException as exc:
                replacement_result["submit"] = exc.status_code

        t1 = threading.Thread(target=replace_worker)
        t2 = threading.Thread(target=submit_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with store.read() as conn:
            phase = conn.execute(
                "SELECT phase FROM batches WHERE id = ?", (bid,)
            ).fetchone()["phase"]

        if phase == "sealed":
            # submission won: replacement must have been rejected
            assert replacement_result.get("error") == 409
            assert replacement_result.get("submit") == 201
            assert "ok" not in replacement_result
            assert submit(client, bid, r2, AGREE_LABELS).status_code == 409
        else:
            # replacement won: batch collecting, only reviewer 1 submitted,
            # old token dead (401: credential check precedes phase check),
            # new token able to submit and then seal
            assert phase == "collecting"
            assert replacement_result.get("submit") == 401
            new_token = replacement_result["ok"]
            assert submit(client, bid, r2, AGREE_LABELS).status_code == 401
            r = submit(client, bid, new_token, AGREE_LABELS)
            assert r.status_code == 201 and r.json()["phase"] == "sealed"


def test_interleaved_replacement_and_disagreeing_second_submission(tmp_path):
    """Replacement races the second submission when it would disagree.

    Both linearizations converge on the same state once serialized:
    * replacement first: the old-credential submit is rejected 401 (token
      already dead), the batch keeps only reviewer 1's submission;
    * submission first: the disagreement moves the batch to adjudicating,
      then the replacement voids seat 2 and takes it back to collecting.
    Either way the batch ends collecting with exactly one submission, the old
    credential dead and the successor able to recreate the disagreement.
    """
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = make_batch(client)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        r1, r2 = batch["reviewer1_token"], batch["reviewer2_token"]
        submit(client, bid, r1, R1_LABELS)

        outcome = {}

        def replace_worker():
            out = logic_replace_reviewer(
                store, bid, arb, ReviewerReplacementRequest(reviewer=2)
            )
            outcome["new_token"] = out["reviewer_token"]

        def submit_worker():
            try:
                logic_submit(store, bid, r2, R2_LABELS)
                outcome["submit"] = 201
            except HTTPException as exc:
                outcome["submit"] = exc.status_code

        t1 = threading.Thread(target=replace_worker)
        t2 = threading.Thread(target=submit_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # replacement never fails here (a disagreeing submit never seals);
        # the submit lands at 201 only when it beats the replacement
        assert outcome["submit"] in (201, 401)
        new_token = outcome["new_token"]
        with store.read() as conn:
            phase = conn.execute(
                "SELECT phase FROM batches WHERE id = ?", (bid,)
            ).fetchone()["phase"]
            n_subs = conn.execute(
                "SELECT COUNT(*) AS c FROM submissions WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
            n_adj = conn.execute(
                "SELECT COUNT(*) AS c FROM adjudications WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
        assert phase == "collecting"
        assert n_subs == 1 and n_adj == 0

        # old credential dead; the successor restores the adjudicating round
        assert submit(client, bid, r2, R2_LABELS).status_code == 401
        r = submit(client, bid, new_token, R2_LABELS)
        assert r.status_code == 201 and r.json()["phase"] == "adjudicating"
        r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": arb})
        assert [d["code"] for d in r.json()["disagreements"]] == ["S03"]


def test_interleaved_replacement_and_adjudication(tmp_path):
    """A replacement racing an adjudication: exactly one terminal outcome.

    Adjudication commits first -> the batch is sealed and the replacement is
    rejected 409. Replacement commits first -> back to collecting with the
    adjudicating reviewer's submission voided, and the arbitration is rejected
    409; no adjudication row survives.
    """
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = make_batch(client)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        submit(client, bid, batch["reviewer1_token"], R1_LABELS)
        submit(client, bid, batch["reviewer2_token"], R2_LABELS)
        assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"

        outcome = {}

        def replace_worker():
            try:
                logic_replace_reviewer(
                    store, bid, arb, ReviewerReplacementRequest(reviewer=2)
                )
                outcome["replaced"] = True
            except HTTPException as exc:
                outcome["replaced"] = exc.status_code

        def adjudicate_worker():
            try:
                logic_adjudicate(store, bid, arb, {"S03": "bird"})
                outcome["adjudicated"] = True
            except HTTPException as exc:
                outcome["adjudicated"] = exc.status_code

        threads = [threading.Thread(target=replace_worker),
                   threading.Thread(target=adjudicate_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with store.read() as conn:
            phase = conn.execute(
                "SELECT phase FROM batches WHERE id = ?", (bid,)
            ).fetchone()["phase"]
            n_finals = conn.execute(
                "SELECT COUNT(*) AS c FROM finals WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
            n_adj = conn.execute(
                "SELECT COUNT(*) AS c FROM adjudications WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]

        if phase == "sealed":
            assert outcome == {"replaced": 409, "adjudicated": True}
            assert n_finals == 3 and n_adj == 1
        else:
            assert phase == "collecting"
            assert outcome == {"replaced": True, "adjudicated": 409}
            assert n_finals == 0 and n_adj == 0


# --------------------------------------------------------------------------
# Persistence and re-evaluation batches
# --------------------------------------------------------------------------

def test_replacement_survives_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    app1 = create_app(db)
    with TestClient(app1) as client1:
        batch = make_batch(client1)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        submit(client1, bid, batch["reviewer1_token"], R1_LABELS)
        submit(client1, bid, batch["reviewer2_token"], R2_LABELS)
        body = replace(client1, bid, arb, 2).json()
        assert body["voided_submission"] is True
        successor = body["reviewer_token"]
        # old credential is dead before the restart
        assert submit(client1, bid, batch["reviewer2_token"], R2_LABELS).status_code == 401

    app2 = create_app(db)
    with TestClient(app2) as client2:
        assert client2.get(f"/batches/{bid}").json()["phase"] == "collecting"
        assert submit(client2, bid, batch["reviewer2_token"], R2_LABELS).status_code == 401
        r = submit(client2, bid, successor, R2_LABELS)
        assert r.status_code == 201 and r.json()["phase"] == "adjudicating"
        # surviving reviewer 1 submission is still in place after restart
        own = client2.get(
            f"/batches/{bid}/submission",
            headers={"X-Reviewer-Token": batch["reviewer1_token"]},
        ).json()
        assert own["submitted"] is True and own["labels"] == R1_LABELS


def test_replacement_works_on_a_reevaluation_batch(client):
    """Re-evaluation batches are ordinary batches for succession purposes."""
    # seal an ordinary batch first so a re-evaluation can be opened
    source = make_batch(client)
    sid = source["batch_id"]
    submit(client, sid, source["reviewer1_token"], R1_LABELS)
    submit(client, sid, source["reviewer2_token"], R2_LABELS)
    adjudicate(client, sid, source["arbiter_token"], {"S03": "bird"})

    reeval = client.post(
        f"/batches/{sid}/reevaluations",
        json={"truth_revision": 0},
        headers={"X-Arbiter-Token": source["arbiter_token"]},
    ).json()
    nid = reeval["batch_id"]
    submit(client, nid, reeval["reviewer1_token"], R1_LABELS)

    body = replace(client, nid, reeval["arbiter_token"], 2).json()
    assert body["phase"] == "collecting" and body["voided_submission"] is False
    successor = body["reviewer_token"]
    assert submit(client, nid, reeval["reviewer2_token"], R2_LABELS).status_code == 401
    r = submit(client, nid, successor, R2_LABELS)
    assert r.status_code == 201 and r.json()["phase"] == "adjudicating"
    # source credentials still do not operate the new round
    assert replace(client, nid, source["arbiter_token"], 1).status_code == 401
