"""Integration, end-to-end and security tests against a real Postgres."""
import pytest

from core import db, jobs, scenario
from sp import world
from sp.seed import TENANTS

from .conftest import bearer

PROC, ANALYST, ADMIN, OTHER = bearer("acme", "procurement"), bearer("acme", "analyst"), bearer("acme", "admin"), bearer("other", "procurement")


def test_models_beat_their_baselines(seeded):
    m = seeded["metrics"]
    assert m["entity_resolution"]["model"]["f1"] > m["entity_resolution"]["baseline"]["f1"] and m["entity_resolution"]["model"]["precision"] > 0.99
    assert m["classification"]["model"]["accuracy"] > m["classification"]["baseline"]["accuracy"] + 0.1


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


@pytest.fixture(scope="module")
def truth():
    return world.build(TENANTS["acme"][1], TENANTS["acme"][2])


def test_demo_journey_matches_what_was_planted(demo, truth):
    year = [x for x in truth["invoices"] if x["year"] == 2]
    imp = demo["imp"]["result"]
    assert imp["invoices"] == len(year) and imp["spend"] == pytest.approx(sum(x["total"] for x in year), abs=1)
    res = demo["res"]
    assert abs(res["canonical_vendors"] - len(truth["vendors"])) <= 0.03 * len(truth["vendors"]) and res["vendor_records"] == len(truth["aliases"])
    assert len(demo["cloud"]["aliases"]) == 4 and all(a["match_confidence"] > 0.9 for a in demo["cloud"]["aliases"])
    cls = demo["cls"]["result"]
    assert cls["classified"] == imp["uncategorized_lines"] and cls["mean_confidence"] > 0.9
    assert sum(r["amount"] for r in demo["cube"]["row_totals"]) == pytest.approx(imp["spend"], abs=1) and "Uncategorized" not in {r["key"] for r in demo["cube"]["row_totals"]}
    planted = [x for x in year if x["dup_of"] is not None]
    d = demo["dup"]
    assert abs(d["pairs"] - len(planted)) <= 3 and d["amount"] == pytest.approx(sum(x["total"] for x in planted), rel=0.03) and d["baseline"]["pairs"] < 0.5 * d["pairs"]
    terms = {(t["term_type"], t["key"]) for t in demo["ext"]["terms"]}
    assert ("rate_card", "Senior Consultant") in terms and all(t["quote"] in demo["ext"]["document"] for t in demo["ext"]["terms"])
    leak = {k["kind"]: k for k in demo["leak"]["by_kind"]}
    over = sum(ln["overbilled"] for x in year for ln in (truth["lines"][i] for i in x["lines"]))
    assert float(leak["rate_above_contract"]["amount"]) == pytest.approx(over, rel=0.01)
    owed = sum(r["owed"] for r in truth["rebates"] if r["year"] == 2 and not r["credited"])
    assert float(leak["missed_rebate"]["amount"]) == pytest.approx(owed, rel=0.05)
    assert demo["leak"]["annualized"] == pytest.approx(demo["leak"]["total"], rel=0.01) and all(f["citation_anchor"] and f["quote"] for f in demo["leak"]["top_findings"])
    cons = demo["opp"]["consolidation"]
    assert len(cons["evidence"]["vendors"]) == 6 and cons["evidence"]["category"] == "Collaboration software" and float(cons["estimated_savings"]) == pytest.approx(0.12 * cons["evidence"]["spend_moved"], abs=1)
    assert demo["opp"]["items"][0]["opportunity_type"] == "recover_duplicate_payments"
    dos = demo["dos"]
    assert dos["leakage"] and len(dos["rate_card_vs_billed"]) == 4 and all(float(r["max_billed"]) >= float(r["contract_rate"]) for r in dos["rate_card_vs_billed"]) and len(dos["alternatives"]) >= 3
    assert demo["audit"]["chain_valid"] and {"import.completed", "vendors.resolved", "spend.classified", "contract.extracted", "leakage.scanned"} <= {e["action"] for e in demo["audit"]["events"]}


def test_reimport_adds_nothing_and_resolution_is_stable(client, demo):
    with db.tx() as c:
        before = c.execute("SELECT (SELECT count(*) FROM invoices) i, (SELECT count(*) FROM invoice_lines) l, (SELECT count(*) FROM vendor_aliases) a").fetchone()
    job = client.post("/v1/imports", headers=PROC, json={"fiscal_year": "latest"}).json()
    jobs.drain()
    assert client.get(job["status_url"], headers=PROC).json()["result"]["invoices"] == 0
    with db.tx() as c:
        assert c.execute("SELECT (SELECT count(*) FROM invoices) i, (SELECT count(*) FROM invoice_lines) l, (SELECT count(*) FROM vendor_aliases) a").fetchone() == before
    vid = demo["cloudv"]["items"][0]["id"]
    again = client.post("/v1/vendors:resolve", headers=PROC, json={}).json()
    assert again["canonical_vendors"] == demo["res"]["canonical_vendors"] and client.get(f"/v1/vendors/{vid}/dossier", headers=ANALYST).status_code == 200      # vendor ids survive a re-run


def test_a_human_split_is_remembered(client, demo):
    a, b = (x["alias_id"] for x in client.post("/v1/vendors:resolve", headers=PROC, json={}).json()["largest_merges"][0]["aliases"][:2])
    n = demo["res"]["canonical_vendors"]
    out = client.post("/v1/vendors:resolve", headers=PROC, json={"decisions": [{"alias_a": a, "alias_b": b, "same": False}]}).json()
    assert out["human_decisions_applied"] == 1 and out["canonical_vendors"] >= n
    assert client.post("/v1/vendors:resolve", headers=PROC, json={}).json()["human_decisions_applied"] == 1
    client.post("/v1/vendors:resolve", headers=PROC, json={"decisions": [{"alias_a": a, "alias_b": b, "same": True}]})


@pytest.mark.parametrize("headers,method,path,body,expected", [
    ({}, "GET", "/v1/leakage", None, 401),
    (ANALYST, "POST", "/v1/imports", {}, 403),
    (ANALYST, "POST", "/v1/vendors:resolve", {}, 403),
    (ANALYST, "GET", "/v1/audit", None, 403),
    (ANALYST, "GET", "/v1/leakage", None, 200),
    (ADMIN, "GET", "/v1/audit", None, 200),
    (ANALYST, "GET", "/v1/spend/cube?rows=vendor;DROP TABLE invoices", None, 422),
    (ANALYST, "GET", "/v1/spend/cube?rows=category&cols=1=1", None, 422),
])
def test_authorization_and_dimension_whitelist(client, demo, headers, method, path, body, expected):
    assert client.request(method, path, headers=headers, json=body).status_code == expected


def test_tenant_isolation(client, demo):
    vid, kid = demo["firm"]["items"][0]["id"], demo["ks"]["items"][0]["id"]
    assert client.get(f"/v1/vendors/{vid}/dossier", headers=OTHER).status_code == 404
    assert client.post(f"/v1/contracts/{kid}:extract", headers=OTHER).status_code == 404
    assert client.post("/v1/vendors:resolve", headers=OTHER, json={}).json()["code"] == "nothing_imported"
    assert client.get("/v1/leakage", headers=OTHER).json()["spend"] == 0 and client.get("/v1/vendors?q=a", headers=OTHER).json()["items"] == []
    job = client.post("/v1/imports", headers=OTHER, json={}).json()
    jobs.drain()
    theirs = client.get(job["status_url"], headers=OTHER).json()["result"]
    assert 0 < theirs["invoices"] < demo["imp"]["result"]["invoices"] / 5
    assert client.get("/v1/spend/cube", headers=ANALYST).json()["total"] == pytest.approx(demo["cube"]["total"], abs=1)      # their import did not touch this tenant's spend
