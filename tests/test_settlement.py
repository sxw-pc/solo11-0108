"""Business decisions of the independent payment settlement ledger."""
import json
from contextlib import closing
from pathlib import Path

import pytest

from keybridge import settlement
from keybridge.settlement_store import connect


FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def feed(tmp_path, db, payload, name="feed.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return settlement.import_file(db, path)


def by_id(rows, key):
    return {row[key]: row for row in rows}


def test_split_merge_and_idempotent_evidence(tmp_path):
    db = tmp_path / "ledger.db"
    payload = load("settlement_ok.json")
    first = feed(tmp_path, db, payload)
    result = settlement.report(db)
    assert first["status"] == "settled"
    assert len(result["edges"]) == 4
    assert {(e["payment_id"], e["invoice_id"], e["cents"]) for e in result["edges"]} == {
        ("P1", "I1", 8000), ("P1", "I2", 4000), ("P2", "I1", 2000), ("P2", "I2", 1000)
    }
    assert {i["state"] for i in result["invoices"]} == {"settled"}
    assert len(result["source_files"]) == 1
    assert feed(tmp_path, db, payload)["idempotent"] is True
    assert len(settlement.report(db)["history"]) == 1


def test_partial_payment_and_late_full_reversal_preserve_old_decision(tmp_path):
    db = tmp_path / "ledger.db"
    initial = load("settlement_ok.json")
    partial = {**initial, "payments": initial["payments"][:1], "allocations": initial["allocations"][:2]}
    assert feed(tmp_path, db, partial)["status"] == "open"
    invoice = by_id(settlement.report(db)["invoices"], "invoice_id")
    assert (invoice["I1"]["state"], invoice["I1"]["outstanding_cents"]) == ("partial", 2000)
    assert (invoice["I2"]["state"], invoice["I2"]["outstanding_cents"]) == ("partial", 1000)
    assert feed(tmp_path, db, {"payments": initial["payments"][1:], "allocations": initial["allocations"][2:]}, "second.json")["status"] == "settled"
    assert feed(tmp_path, db, load("settlement_reversal.json"), "reversal.json")["status"] == "open"
    report = settlement.report(db)
    invoice = by_id(report["invoices"], "invoice_id")
    assert (invoice["I1"]["paid_cents"], invoice["I2"]["paid_cents"]) == (8000, 4000)
    assert [h["status"] for h in report["history"]] == ["open", "settled", "open"]
    assert by_id(report["payments"], "payment_id")["P2"]["state"] == "reversed"


def test_attributed_partial_reversal_only_changes_its_allocation(tmp_path):
    db = tmp_path / "ledger.db"
    feed(tmp_path, db, load("settlement_ok.json"))
    result = feed(tmp_path, db, load("settlement_attributed_reversal.json"), "reverse.json")
    assert result["status"] == "open"
    invoice = by_id(settlement.report(db)["invoices"], "invoice_id")
    assert (invoice["I1"]["paid_cents"], invoice["I2"]["paid_cents"]) == (8000, 5000)


def test_unattributed_partial_reversal_of_split_payment_is_disputed(tmp_path):
    db = tmp_path / "ledger.db"
    feed(tmp_path, db, load("settlement_ok.json"))
    reverse = load("settlement_attributed_reversal.json")
    reverse["reversals"][0].pop("invoice_id")
    result = feed(tmp_path, db, reverse, "reverse.json")
    assert result["status"] == "disputed"
    report = settlement.report(db)
    assert "ambiguous_reversal" in {issue["code"] for issue in report["issues"]}
    assert by_id(report["payments"], "payment_id")["P1"]["state"] == "disputed"


def test_equal_totals_do_not_hide_ambiguous_subset(tmp_path):
    db = tmp_path / "ledger.db"
    payload = load("settlement_ambiguous.json")
    assert sum(settlement.cents(i["amount"]) for i in payload["invoices"]) == sum(settlement.cents(p["amount"]) for p in payload["payments"])
    result = feed(tmp_path, db, payload)
    assert result["status"] == "disputed"
    report = settlement.report(db)
    assert report["edges"] == []
    assert "ambiguous_match" in {issue["code"] for issue in report["issues"]}
    assert {r["state"] for r in report["invoices"]} == {"disputed"}


def test_equal_totals_wrong_party_remains_unmatched(tmp_path):
    db = tmp_path / "ledger.db"
    result = feed(tmp_path, db, load("settlement_wrong_party.json"))
    assert result["status"] == "open"
    report = settlement.report(db)
    invoice = by_id(report["invoices"], "invoice_id")
    assert invoice["WB"]["state"] == "unpaid"
    payment = by_id(report["payments"], "payment_id")["WP"]
    assert (payment["state"], payment["net_cents"], payment["allocated_cents"]) == ("partial", 15000, 10000)


def test_unmatched_extra_payment_cannot_make_fully_paid_book_settled(tmp_path):
    db = tmp_path / "ledger.db"
    payload = load("settlement_ok.json")
    payload["payments"].append({"event_id": "bank-extra", "id": "P3", "party": "elsewhere", "reference": "R", "currency": "CNY", "amount": "1.00", "at": "2025-01-03"})
    result = feed(tmp_path, db, payload)
    assert result["status"] == "disputed"
    report = settlement.report(db)
    assert all(i["state"] == "settled" for i in report["invoices"])
    assert by_id(report["payments"], "payment_id")["P3"]["state"] == "disputed"


def test_unique_inferred_subset_and_single_partial(tmp_path):
    db = tmp_path / "ledger.db"
    payload = load("settlement_ok.json")
    payload["payments"] = [{**payload["payments"][0], "amount": "150.00"}]
    payload["allocations"] = []
    assert feed(tmp_path, db, payload)["status"] == "settled"
    assert {(e["invoice_id"], e["cents"], e["source"]) for e in settlement.report(db)["edges"]} == {
        ("I1", 10000, "inferred"), ("I2", 5000, "inferred")
    }
    other = tmp_path / "other.db"
    payload["invoices"] = payload["invoices"][:1]
    payload["payments"][0]["amount"] = "40.00"
    assert feed(tmp_path, other, payload, "partial.json")["status"] == "open"
    assert settlement.report(other)["invoices"][0]["outstanding_cents"] == 6000


def test_event_id_bound_to_kind_and_amount_conflicts_rollback(tmp_path):
    db = tmp_path / "ledger.db"
    original = load("settlement_ok.json")
    feed(tmp_path, db, original)
    reused = {"payments": [original["invoices"][0]]}
    with pytest.raises(ValueError, match="type conflict"):
        feed(tmp_path, db, reused, "kind.json")
    changed = {"invoices": [{**original["invoices"][0], "amount": "101.00"}]}
    with pytest.raises(ValueError, match="conflict"):
        feed(tmp_path, db, changed, "amount.json")
    assert len(settlement.report(db)["history"]) == 1
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM settlement_file").fetchone()[0] == 1


def test_non_cent_amount_is_rejected_without_partial_import(tmp_path):
    db = tmp_path / "ledger.db"
    payload = load("settlement_ok.json")
    payload["invoices"][0]["amount"] = "0.001"
    with pytest.raises(ValueError, match="whole cents"):
        feed(tmp_path, db, payload)
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM settlement_event").fetchone()[0] == 0
