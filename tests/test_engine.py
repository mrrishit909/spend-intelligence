"""Unit tests for the pure engines: name normalisation, pair evidence, duplicates, contract terms, leakage, scoring."""
import datetime

import pytest

from sp import engine, world

D = datetime.date


def test_name_normalisation():
    assert engine.normalize("WREN CLOUD SVCS LTD.") == engine.normalize("Wren Cloud Services Limited") == "wren cloud services"
    assert engine.normalize("Falcon Freight, Inc.") == "falcon freight" and engine.normalize("Alder & Co") == "alder &"


def test_pair_evidence_missing_is_neutral_and_conflict_is_negative():
    a = {"norm": "wren cloud services", "tax_id": "t1", "bank": None, "address": "12 Elm Street"}
    b = {"norm": "wren cloud services", "tax_id": "t1", "bank": "b9", "address": "12 ELM ST"}
    c = {"norm": "wren cleaning services", "tax_id": "t2", "bank": "b7", "address": "99 Oak Street"}
    assert engine.pair_features(a, b) == [1.0, 1.0, 1.0, 0.0, 1.0]
    f = engine.pair_features(a, c)
    assert f[0] == pytest.approx(0.5) and f[2] == -1.0 and f[3] == 0.0 and f[4] == -0.5


def test_resolver_merges_spellings_keeps_lookalikes_apart_and_obeys_people():
    w = world.build(0.3, seed=5)
    truth = [a["vendor"] for a in w["aliases"]]
    model = engine.train_resolver(w["aliases"], truth)
    recs = [{"raw_name": "Wren Cloud Services Ltd", "tax_id": "t1", "bank": "b1", "address": "1 A Street"}, {"raw_name": "WREN CLOUD SVCS LTD.", "tax_id": "t1", "bank": None, "address": None},
            {"raw_name": "Wren Clod Services Limited", "tax_id": None, "bank": "b1", "address": "1 A ST"}, {"raw_name": "Wren Cleaning Services Ltd", "tax_id": "t2", "bank": "b2", "address": "7 B Street"}]
    cl, proba = engine.resolve(recs, model)
    assert cl[0] == cl[1] == cl[2] != cl[3]
    split, _ = engine.resolve(recs, model, {(0, 1): False, (1, 2): False})        # a person says record 1 is someone else
    assert split[1] != split[0] and split[0] == split[2]
    joined, _ = engine.resolve(recs, model, {(0, 3): True})                       # ...or insists two are the same
    assert joined[0] == joined[3]
    base, _ = engine.resolve(recs)                                                # the exact-name baseline misses the typo
    assert base[0] == base[1] and base[2] != base[0]
    assert engine.pair_scores([0, 0, 1, 1], [0, 0, 0, 1]) == {"precision": 0.5, "recall": 0.3333, "f1": 0.4}


def test_invoice_number_key_and_duplicate_rules():
    assert {engine.number_key(n) for n in ("INV-00123", "123", "00123-A", "INV-OO123", "123 ")} == {"123"}
    inv = lambda i, vendor, alias, number, day, total: {"id": i, "vendor_id": vendor, "alias_id": alias, "number": number, "date": D(2026, 3, day), "total": total}      # noqa: E731
    book = [inv(1, "v", "a1", "INV-00123", 1, 500.0), inv(2, "v", "a2", "123", 6, 500.0),          # same vendor under another spelling, number retyped
            inv(3, "v", "a1", "INV-00124", 6, 500.0),                                              # the next invoice for the same amount: not a duplicate
            inv(4, "v", "a1", "INV-00123", 28, 500.0),                                             # same number but four weeks later: outside the window
            inv(5, "x", "a9", "123", 2, 500.0),                                                    # another vendor
            inv(6, "v", "a1", "CN-1", 3, -500.0)]                                                  # a credit note
    found = engine.duplicates(book)
    assert [(d["original"], d["duplicate"]) for d in found] == [(1, 2)] and found[0]["cross_spelling"] and found[0]["days_apart"] == 5
    assert engine.duplicates(book, by="alias_id") == []                                            # the usual same-record check sees nothing


def test_contract_terms_carry_their_clause_and_sentence():
    w = world.build(0.3, seed=5)
    for k in w["contracts"]:
        terms = engine.extract_terms(k["text"])
        got = {t["key"]: t["value"] for t in terms if t["term_type"] == "rate_card"}
        assert got == (k["rates"] or {})
        if k["rebate"]:
            reb = {t["key"]: t for t in terms if t["term_type"] == "rebate"}
            assert (reb["threshold"]["value"], reb["percent"]["value"]) == (float(k["rebate"][0]), float(k["rebate"][1])) and reb["percent"]["citation_anchor"] == "§4 Volume Rebate"
        one = {(t["term_type"], t["key"]): t["value"] for t in terms}
        assert one[("payment", "net_days")] == k["net_days"] and one[("price_cap", "max_increase_percent")] == k["price_cap_pct"] and one[("term", "end_date")] == k["end"].isoformat()
        assert all(t["quote"] in k["text"] and t["citation_anchor"] for t in terms)               # every term can be checked against the document
    assert engine.extract_terms("A letter with no numbered clauses. Senior Consultant: $900.00 per hour") == []


def test_rate_and_rebate_leakage():
    card = {"Senior Consultant": (185.0, "t1"), "Consultant": (140.0, "t2")}
    lines = [{"id": 1, "description": "Senior Consultant - 40 hrs @ 210.00", "qty": 40, "unit_price": 210.0}, {"id": 2, "description": "Consultant - 8 hrs @ 140.00", "qty": 8, "unit_price": 140.0},
             {"id": 3, "description": "Contractor Consultant week ending 16 hrs @ 150.00", "qty": 16, "unit_price": 150.0}, {"id": 4, "description": "travel expenses", "qty": 1, "unit_price": 900.0},
             {"id": 5, "description": "Principal - 8 hrs @ 999.00", "qty": 8, "unit_price": 999.0}]
    out = {f["invoice_line_id"]: f for f in engine.rate_leakage(lines, card)}
    assert set(out) == {1, 3} and out[1]["variance"] == 1000.0 and out[1]["contract_term_id"] == "t1" and out[3]["variance"] == 160.0      # 'Senior Consultant' is not matched as 'Consultant'
    assert engine.rebate_leakage(90_000, 0, 100_000, 5) is None
    assert engine.rebate_leakage(200_000, 0, 100_000, 5)["variance"] == 10_000 and engine.rebate_leakage(200_000, 10_000, 100_000, 5) is None
    assert engine.rebate_leakage(200_000, 4_000, 100_000, 5)["variance"] == 6_000


def test_classifier_and_scoring():
    assert engine.keyword_category("LTL FREIGHT SHIPMENT ref 1234") == "Freight" and engine.keyword_category("mystery") == "Office supplies"
    assert engine.score(100_000, 0.9, "low") == 90_000 and engine.score(100_000, 0.5, "high") == 12_500
    assert engine.score(100_000, 0.5, "high", months_to_renewal=3) > engine.score(100_000, 0.5, "high", months_to_renewal=18)


def test_world_plants_what_it_says():
    w = world.build(0.2, seed=7)
    assert len([v for v in w["vendors"] if v["category"] == "Collaboration software"]) == 6
    assert len(next(v for v in w["vendors"] if v["category"] == "Cloud infrastructure")["aliases"]) == 4
    dups = [x for x in w["invoices"] if x["dup_of"] is not None]
    assert dups and all(x["total"] == w["invoices"][x["dup_of"]]["total"] and x["vendor"] == w["invoices"][x["dup_of"]]["vendor"] for x in dups)
    assert all(abs(sum(w["lines"][i]["amount"] for i in x["lines"]) - x["total"]) < 0.01 for x in w["invoices"])
