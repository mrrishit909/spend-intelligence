"""Public API (modular monolith). Blueprint services map to: connector-service (import job), vendor-resolution, taxonomy-service,
invoice-matcher, contract-extractor, leakage-engine, opportunity-engine (sp/engine.py), supplier-intelligence and analytics-api (this file).

    uvicorn sp.api:app          python -m core.jobs sp.api      # the worker
"""
import datetime
import hashlib
import pickle
import uuid

from fastapi import Depends, Header
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world
from .seed import TENANTS, cat_id

READ = {"spend:read", "jobs:read"}
PERMISSIONS = {"analyst": READ, "procurement": READ | {"imports:write", "vendors:resolve", "contracts:write", "leakage:scan"},
               "admin": READ | {"imports:write", "vendors:resolve", "contracts:write", "leakage:scan", "audit:read"}}
app = create_app("spend-intelligence", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
_models, _cache = {}, {}
CONSOLIDATION_RATE = 0.12          # assumed saving on spend moved to a preferred vendor; an assumption, shown as such
DIMS = {"category": "coalesce(c.name, 'Uncategorized')", "parent_category": "coalesce(p.name, 'Uncategorized')", "vendor": "coalesce(v.canonical_name, a.raw_name)", "business_unit": "i.business_unit",
        "quarter": "to_char(i.invoice_date, 'YYYY-\"Q\"Q')", "month": "to_char(i.invoice_date, 'YYYY-MM')", "source_system": "a.source_system"}


def models():
    if "m" not in _models:
        with db.tx() as c:
            _models["m"] = pickle.loads(c.execute("SELECT artifact FROM model_registry WHERE name = 'spend-models' AND state = 'production'").fetchone()["artifact"])      # our own training output
    return _models["m"]


def sha(v):
    return hashlib.sha256(f"demo-salt|{v}".encode()).hexdigest()[:24] if v else None


def system_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


# ---------------------------------------------------------------- connector-service

class ImportIn(BaseModel):
    source: str = Field("erp-extract", pattern="^erp-extract$", description="The one connector in this build: a deterministic synthetic ERP / AP / card extract")
    fiscal_year: str = Field("latest", pattern="^(latest|previous|oldest)$")


@app.post("/v1/imports", status_code=202, tags=["imports"], summary="Start an ERP / AP / card import (job)")
def start_import(body: ImportIn, ctx: Ctx = Depends(auth("imports:write")), idem: str | None = IdemKey):
    return run(ctx, idem, body, lambda c: (202, jobs.enqueue(c, ctx, "import.run", body.model_dump())))


@jobs.handler("import.run")
def import_job(c, job):
    t = job["tenant_id"]
    name = c.execute("SELECT name FROM tenants WHERE id = %s", [t]).fetchone()["name"]
    scale, seed = next((s, d) for n, s, d in TENANTS.values() if n == name)
    w = world.build(scale, seed)
    year = {"latest": 2, "previous": 1, "oldest": 0}[job["payload"]["fiscal_year"]]
    al = [uuid.uuid5(t, f"alias-{i}") for i in range(len(w["aliases"]))]
    db.many(c, """INSERT INTO vendor_aliases (id, source_system, source_vendor_id, raw_name, tenant_id, tax_id_hash, bank_hash, address) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (tenant_id, source_system, source_vendor_id) DO NOTHING""",
            [(al[i], a["source_system"], a["source_vendor_id"], a["raw_name"], t, sha(a["tax_id"]), sha(a["bank"]), a["address"]) for i, a in enumerate(w["aliases"])])
    invs = [x for x in w["invoices"] if x["year"] == year]
    if c.execute("SELECT 1 FROM invoices WHERE id = %s", [invs[0]["id"]]).fetchone():
        return {"fiscal_year": job["payload"]["fiscal_year"], "invoices": 0, "invoice_lines": 0, "note": "this period was already imported; nothing added"}
    po_ids = {x["po"] for x in invs if x["po"] is not None}
    db.load(c, "purchase_orders", ["id", "tenant_id", "po_number", "ordered_at", "category_id"], [(w["pos"][i]["id"], t, w["pos"][i]["number"], w["pos"][i]["date"], cat_id(w["pos"][i]["category"])) for i in sorted(po_ids)])
    db.load(c, "invoices", ["id", "tenant_id", "invoice_number", "invoice_date", "due_date", "total", "po_id", "fingerprint", "alias_id", "business_unit"],
            [(x["id"], t, x["number"], x["date"], x["due"], x["total"], w["pos"][x["po"]]["id"] if x["po"] is not None else None, f"{engine.number_key(x['number'])}|{round(x['total'] * 100)}", al[x["alias"]], x["unit"]) for x in invs])
    lines = [w["lines"][i] for x in invs for i in x["lines"]]
    db.copy(c, "invoice_lines", ["id", "invoice_id", "description", "qty", "unit_price", "amount", "category_id", "category_source"],
            [(ln["id"], w["invoices"][ln["invoice"]]["id"], ln["description"], ln["qty"], ln["unit_price"], ln["amount"], cat_id(ln["source_category"]) if ln["source_category"] else None,
              "source" if ln["source_category"] else None) for ln in lines])
    db.many(c, "INSERT INTO contracts (id, tenant_id, title, start_date, end_date, object_uri, status, alias_id, body) VALUES (%s,%s,%s,%s,%s,%s,'received',%s,%s) ON CONFLICT DO NOTHING",
            [(k["id"], t, k["title"], k["start"], k["end"], f"inline:{k['id']}", al[w["vendors"][k["vendor"]]["aliases"][0]], k["text"]) for k in w["contracts"]])
    audit.record(c, system_ctx(job), "import.completed", "tenant", t, {"fiscal_year": job["payload"]["fiscal_year"], "invoices": len(invs), "lines": len(lines)})
    return {"fiscal_year": job["payload"]["fiscal_year"], "period": [min(x["date"] for x in invs), max(x["date"] for x in invs)], "vendor_records": len(al), "source_systems": 3, "invoices": len(invs),
            "invoice_lines": len(lines), "purchase_orders": len(po_ids), "contracts": len(w["contracts"]), "spend": round(sum(x["total"] for x in invs), 2),
            "uncategorized_lines": sum(ln["source_category"] is None for ln in lines)}


# ---------------------------------------------------------------- vendor-resolution

class Decision(BaseModel):
    alias_a: uuid.UUID
    alias_b: uuid.UUID
    same: bool


class ResolveIn(BaseModel):
    decisions: list[Decision] = Field(default_factory=list, max_length=200, description="Human merge / split decisions; they override the model and are remembered")


@app.post("/v1/vendors:resolve", tags=["vendors"], summary="Run supplier entity resolution (and record any human merge / split decisions)")
def resolve(body: ResolveIn, ctx: Ctx = Depends(auth("vendors:resolve")), idem: str | None = IdemKey):
    def work(c):
        for d in body.decisions:
            a, b = sorted([d.alias_a, d.alias_b], key=str)
            c.execute("""INSERT INTO vendor_decisions (tenant_id, alias_a, alias_b, same, decided_by) VALUES (%s,%s,%s,%s,%s)
                         ON CONFLICT (alias_a, alias_b) DO UPDATE SET same = excluded.same, decided_by = excluded.decided_by, decided_at = now()""", [ctx.tenant_id, a, b, d.same, ctx.actor_id])
        rows = c.execute("SELECT id, raw_name, source_system, tax_id_hash AS tax_id, bank_hash AS bank, address FROM vendor_aliases ORDER BY id").fetchall()
        if not rows:
            raise Problem(409, "nothing_imported", "import AP data first")
        idx = {r["id"]: i for i, r in enumerate(rows)}
        human = {(idx[d["alias_a"]], idx[d["alias_b"]]): d["same"] for d in c.execute("SELECT alias_a, alias_b, same FROM vendor_decisions")}
        cluster, proba = engine.resolve(rows, models()["resolver"], human)
        members = {}
        for i, k in enumerate(cluster):
            members.setdefault(k, []).append(i)
        best = {}
        for (i, j), p in proba.items():
            if cluster[i] == cluster[j]:
                best[i], best[j] = max(best.get(i, 0), p), max(best.get(j, 0), p)
        c.execute("UPDATE vendor_aliases SET vendor_id = NULL")
        c.execute("DELETE FROM vendors")
        vendors, updates = [], []
        for k, ms in members.items():
            norms = [engine.normalize(rows[i]["raw_name"]) for i in ms]
            canon = min((rows[i]["raw_name"] for i in ms), key=lambda n: (-norms.count(engine.normalize(n)), n.isupper(), "  " in n, len(n), n))      # the spelling most records agree on, in mixed case
            vid = uuid.uuid5(ctx.tenant_id, "vendor-" + min(str(rows[i]["id"]) for i in ms))                     # stable across re-runs
            vendors.append((vid, ctx.tenant_id, canon, next((rows[i]["tax_id"] for i in ms if rows[i]["tax_id"]), None)))
            updates += [(vid, round(best.get(i, 1.0), 4), rows[i]["id"]) for i in ms]
        db.load(c, "vendors", ["id", "tenant_id", "canonical_name", "tax_id_hash"], vendors)
        db.many(c, "UPDATE vendor_aliases SET vendor_id = %s, match_confidence = %s WHERE id = %s", updates)
        c.execute("UPDATE invoices i SET vendor_id = a.vendor_id FROM vendor_aliases a WHERE a.id = i.alias_id")
        c.execute("UPDATE contracts k SET vendor_id = a.vendor_id FROM vendor_aliases a WHERE a.id = k.alias_id")
        c.execute("UPDATE purchase_orders po SET vendor_id = i.vendor_id FROM invoices i WHERE i.po_id = po.id")
        merged = sorted((ms for ms in members.values() if len(ms) > 1), key=lambda ms: (-len(ms), rows[ms[0]]["raw_name"]))
        unsure = sorted(((p, i, j) for (i, j), p in proba.items() if 0.2 < p < 0.8), reverse=True)[:8]
        audit.record(c, ctx, "vendors.resolved", "tenant", ctx.tenant_id, {"records": len(rows), "vendors": len(members), "human_decisions": len(human), "model": engine.VERSION})
        return 200, {"vendor_records": len(rows), "canonical_vendors": len(members), "records_merged_away": len(rows) - len(members), "human_decisions_applied": len(human), "model_version": engine.VERSION,
                     "largest_merges": [{"vendor_id": uuid.uuid5(ctx.tenant_id, "vendor-" + min(str(rows[i]["id"]) for i in ms)), "canonical_name": next(v[2] for v in vendors if v[0] == uuid.uuid5(ctx.tenant_id, "vendor-" + min(str(rows[i]["id"]) for i in ms))),
                                         "aliases": [{"alias_id": rows[i]["id"], "raw_name": rows[i]["raw_name"], "source_system": rows[i]["source_system"], "confidence": round(best.get(i, 1.0), 3),
                                                      "has_tax_id": bool(rows[i]["tax_id"]), "has_bank": bool(rows[i]["bank"])} for i in ms]} for ms in merged[:5]],
                     "needs_review": [{"alias_a": rows[i]["id"], "name_a": rows[i]["raw_name"], "alias_b": rows[j]["id"], "name_b": rows[j]["raw_name"], "probability": round(p, 3)} for p, i, j in unsure]}
    return run(ctx, idem, body, work)


@app.get("/v1/vendors", tags=["vendors"], summary="Find canonical vendors by name fragment")
def find_vendors(q: str, ctx: Ctx = Depends(auth("spend:read"))):
    with db.tx(ctx.tenant_id) as c:
        rows = c.execute("""SELECT v.id, v.canonical_name, count(DISTINCT a.id) AS aliases FROM vendors v JOIN vendor_aliases a ON a.vendor_id = v.id
                             WHERE v.canonical_name ILIKE %s GROUP BY v.id ORDER BY aliases DESC, v.canonical_name LIMIT 20""", [f"%{q}%"]).fetchall()
    return jsonable_encoder({"items": rows})


# ---------------------------------------------------------------- taxonomy-service

@app.post("/v1/spend:classify", status_code=202, tags=["spend"], summary="Classify uncategorized invoice lines into the spend taxonomy (job)")
def classify(ctx: Ctx = Depends(auth("vendors:resolve")), idem: str | None = IdemKey):
    return run(ctx, idem, {}, lambda c: (202, jobs.enqueue(c, ctx, "spend.classify", {})))


@jobs.handler("spend.classify")
def classify_job(c, job):
    rows = c.execute("SELECT l.id, l.description, l.amount FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE l.category_id IS NULL").fetchall()
    total = c.execute("SELECT count(*) AS n, coalesce(sum(l.amount), 0) AS amt FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id").fetchone()
    if not rows:
        return {"classified": 0, "note": "no uncategorized lines"}
    clf = models()["classifier"]
    proba = clf.predict_proba([r["description"] for r in rows])
    pick, conf = proba.argmax(axis=1), proba.max(axis=1)
    db.many(c, "UPDATE invoice_lines SET category_id = %s, category_source = 'model', category_confidence = %s WHERE id = %s",
            [(cat_id(clf.classes_[k]), round(float(p), 4), r["id"]) for r, k, p in zip(rows, pick, conf)])
    by = {}
    for r, k in zip(rows, pick):
        by[clf.classes_[k]] = by.get(clf.classes_[k], 0) + float(r["amount"])
    amt = sum(float(r["amount"]) for r in rows)
    audit.record(c, system_ctx(job), "spend.classified", "tenant", job["tenant_id"], {"lines": len(rows), "model": engine.VERSION})
    return {"classified": len(rows), "amount": round(amt, 2), "share_of_lines": round(len(rows) / total["n"], 4), "share_of_spend": round(amt / float(total["amt"]), 4), "low_confidence": int((conf < 0.6).sum()),
            "mean_confidence": round(float(conf.mean()), 4), "model_version": engine.VERSION, "top_categories": [{"category": k, "amount": round(v, 2)} for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:6]],
            "examples": [{"description": rows[i]["description"], "category": clf.classes_[pick[i]], "confidence": round(float(conf[i]), 3)} for i in range(0, min(len(rows), 4000), 800)]}


@app.get("/v1/spend/cube", tags=["spend"], summary="Spend by any two of: category, parent_category, vendor, business_unit, quarter, month, source_system")
def cube(rows: str = "parent_category", cols: str | None = None, limit: int = 15, ctx: Ctx = Depends(auth("spend:read"))):
    if rows not in DIMS or (cols and cols not in DIMS):
        raise Problem(422, "unknown_dimension", f"choose from {sorted(DIMS)}")           # dimensions are looked up in a fixed map, never spliced from input
    sel = f"{DIMS[rows]} AS r" + (f", {DIMS[cols]} AS c" if cols else "")
    with db.tx(ctx.tenant_id) as c:
        data = cached(c, ctx, ("cube", rows, cols), lambda: c.execute(f"""
            SELECT {sel}, round(sum(l.amount), 2) AS amount, count(*) AS lines FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id JOIN vendor_aliases a ON a.id = i.alias_id
              LEFT JOIN vendors v ON v.id = i.vendor_id LEFT JOIN spend_categories c ON c.id = l.category_id LEFT JOIN spend_categories p ON p.id = c.parent_id
             GROUP BY {'1, 2' if cols else '1'}""").fetchall())
    totals = {}
    for d in data:
        totals[d["r"]] = totals.get(d["r"], 0) + float(d["amount"])
    keep = sorted(totals, key=totals.get, reverse=True)[:max(1, min(limit, 100))]
    return jsonable_encoder({"rows": rows, "cols": cols, "total": round(sum(totals.values()), 2), "row_totals": [{"key": k, "amount": round(totals[k], 2)} for k in keep],
                             "cells": [{"row": d["r"], "col": d["c"], "amount": d["amount"]} for d in data if cols and d["r"] in keep]})


# ---------------------------------------------------------------- invoice-matcher

def data_version(c):
    """Changes whenever an import, a resolution or a classification changes what the analytics would return."""
    r = c.execute("""SELECT (SELECT count(*) FROM invoices) AS n, (SELECT count(*) FROM vendors) AS v, (SELECT max(decided_at) FROM vendor_decisions) AS d,
                            (SELECT count(*) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE l.category_id IS NULL) AS u""").fetchone()
    return (r["n"], r["v"], r["d"], r["u"])


def cached(c, ctx, key, compute):
    """Per-process memo for whole-ledger scans: recomputed only when the tenant's data version moves."""
    k, version = (ctx.tenant_id, key), data_version(c)
    if _cache.get(k, (None,))[0] != version:
        _cache[k] = (version, compute())
    return _cache[k][1]


def duplicate_pairs(c, ctx):
    return cached(c, ctx, "duplicates", lambda: _duplicate_pairs(c))


def _duplicate_pairs(c):
    inv = c.execute("SELECT id, vendor_id, alias_id, invoice_number AS number, invoice_date AS date, total::float AS total FROM invoices WHERE total > 0").fetchall()
    for x in inv:
        x["vendor_id"] = x["vendor_id"] or x["alias_id"]          # before resolution every spelling is its own vendor
    return engine.duplicates(inv), engine.duplicates(inv, by="alias_id")


@app.get("/v1/invoices/duplicates", tags=["invoices"], summary="Probable duplicate invoices, with the evidence for each pair")
def get_duplicates(limit: int = 10, ctx: Ctx = Depends(auth("spend:read"))):
    with db.tx(ctx.tenant_id) as c:
        found, naive = duplicate_pairs(c, ctx)
        top = sorted(found, key=lambda d: -d["amount"])[:max(1, min(limit, 100))]
        ids = [d[k] for d in top for k in ("original", "duplicate")]
        info = {r["id"]: r for r in c.execute("""SELECT i.id, i.invoice_number, i.invoice_date, a.raw_name, a.source_system, coalesce(v.canonical_name, a.raw_name) AS vendor
                                                   FROM invoices i JOIN vendor_aliases a ON a.id = i.alias_id LEFT JOIN vendors v ON v.id = i.vendor_id WHERE i.id = ANY(%s)""", [ids])}
    return jsonable_encoder({"pairs": len(found), "amount": round(sum(d["amount"] for d in found), 2), "entered_under_another_spelling": sum(d["cross_spelling"] for d in found),
                             "baseline": {"name": "same vendor record, identical invoice number and amount", "pairs": len(naive), "amount": round(sum(d["amount"] for d in naive), 2)},
                             "top": [{**d, "vendor": info[d["original"]]["vendor"], "original_invoice": {k: info[d["original"]][k] for k in ("invoice_number", "invoice_date", "raw_name", "source_system")},
                                      "duplicate_invoice": {k: info[d["duplicate"]][k] for k in ("invoice_number", "invoice_date", "raw_name", "source_system")}} for d in top]})


# ---------------------------------------------------------------- contract-extractor

def extract_into(c, contract):
    terms = engine.extract_terms(contract["body"])
    c.execute("DELETE FROM contract_terms WHERE contract_id = %s", [contract["id"]])
    db.many(c, "INSERT INTO contract_terms (id, contract_id, term_type, key, value_json, citation_anchor, quote) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(uuid.uuid7(), contract["id"], t["term_type"], t["key"], Jsonb(t["value"]), t["citation_anchor"], t["quote"]) for t in terms])
    c.execute("UPDATE contracts SET status = 'extracted' WHERE id = %s", [contract["id"]])
    return terms


@app.get("/v1/contracts", tags=["contracts"], summary="Contracts for a vendor")
def list_contracts(vendor_id: uuid.UUID, ctx: Ctx = Depends(auth("spend:read"))):
    with db.tx(ctx.tenant_id) as c:
        return jsonable_encoder({"items": c.execute("SELECT id, title, start_date, end_date, status FROM contracts WHERE vendor_id = %s ORDER BY title", [vendor_id]).fetchall()})


@app.post("/v1/contracts/{contract_id}:extract", tags=["contracts"], summary="Extract rate cards and commercial terms, each with its section and the sentence it came from")
def extract(contract_id: uuid.UUID, ctx: Ctx = Depends(auth("contracts:write")), idem: str | None = IdemKey):
    def work(c):
        k = c.execute("SELECT * FROM contracts WHERE id = %s FOR UPDATE", [contract_id]).fetchone()
        if not k:
            raise Problem(404, "contract_not_found")
        terms = extract_into(c, k)
        audit.record(c, ctx, "contract.extracted", "contract", contract_id, {"terms": len(terms), "extractor": engine.VERSION})
        return 200, {"contract_id": contract_id, "title": k["title"], "extractor": engine.VERSION + " (rules; no language model)", "terms": terms, "document": k["body"]}
    return run(ctx, idem, {}, work)


# ---------------------------------------------------------------- leakage-engine

@app.post("/v1/leakage:scan", status_code=202, tags=["leakage"], summary="Extract every contract, then check invoices against rate cards and rebate clauses (job)")
def scan(ctx: Ctx = Depends(auth("leakage:scan")), idem: str | None = IdemKey):
    return run(ctx, idem, {}, lambda c: (202, jobs.enqueue(c, ctx, "leakage.scan", {})))


@jobs.handler("leakage.scan")
def scan_job(c, job):
    t = job["tenant_id"]
    c.execute("DELETE FROM leakage_findings")
    n_terms = n_contracts = 0
    for k in c.execute("SELECT * FROM contracts WHERE vendor_id IS NOT NULL").fetchall():
        if k["status"] != "extracted":
            extract_into(c, k)
        n_contracts += 1
        terms = c.execute("SELECT id, term_type, key, value_json FROM contract_terms WHERE contract_id = %s", [k["id"]]).fetchall()
        n_terms += len(terms)
        card = {x["key"]: (float(x["value_json"]), x["id"]) for x in terms if x["term_type"] == "rate_card"}
        if card:
            lines = c.execute("SELECT l.id, l.description, l.qty, l.unit_price FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE i.vendor_id = %s", [k["vendor_id"]]).fetchall()
            db.many(c, """INSERT INTO leakage_findings (id, invoice_line_id, contract_term_id, expected_amount, actual_amount, variance, confidence, status, tenant_id, vendor_id, kind, detail)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s)""",
                    [(uuid.uuid7(), f["invoice_line_id"], f["contract_term_id"], f["expected_amount"], f["actual_amount"], f["variance"], f["confidence"], t, k["vendor_id"], f["kind"], f["detail"])
                     for f in engine.rate_leakage(lines, card)])
        reb = {x["key"]: x for x in terms if x["term_type"] == "rebate"}
        if reb:
            s = c.execute("""SELECT coalesce(sum(l.amount) FILTER (WHERE l.amount > 0), 0) AS spend, coalesce(-sum(l.amount) FILTER (WHERE l.amount < 0 AND l.description ILIKE '%%rebate%%'), 0) AS credited
                               FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE i.vendor_id = %s""", [k["vendor_id"]]).fetchone()
            f = engine.rebate_leakage(float(s["spend"]), float(s["credited"]), float(reb["threshold"]["value_json"]), float(reb["percent"]["value_json"]))
            if f:
                c.execute("""INSERT INTO leakage_findings (id, contract_term_id, expected_amount, actual_amount, variance, confidence, status, tenant_id, vendor_id, kind, detail)
                             VALUES (%s,%s,%s,%s,%s,%s,'open',%s,%s,'missed_rebate',%s)""",
                          [uuid.uuid7(), reb["percent"]["id"], f["expected_amount"], f["actual_amount"], f["variance"], f["confidence"], t, k["vendor_id"],
                           f"spend ${float(s['spend']):,.0f} is above the ${float(reb['threshold']['value_json']):,.0f} threshold; {float(reb['percent']['value_json']):g}% rebate owed, ${float(s['credited']):,.0f} credited"])
    out = c.execute("SELECT kind, count(*) AS n, round(sum(variance), 2) AS amount FROM leakage_findings GROUP BY kind").fetchall()
    audit.record(c, system_ctx(job), "leakage.scanned", "tenant", t, {"contracts": n_contracts, "terms": n_terms, "findings": sum(o["n"] for o in out), "engine": engine.VERSION})
    return jsonable_encoder({"contracts_read": n_contracts, "terms_extracted": n_terms, "findings": out})


@app.get("/v1/leakage", tags=["leakage"], summary="Contract-versus-invoice leakage findings and their annual total")
def leakage(limit: int = 8, ctx: Ctx = Depends(auth("spend:read"))):
    with db.tx(ctx.tenant_id) as c:
        kinds = c.execute("SELECT kind, count(*) AS findings, round(sum(variance), 2) AS amount, count(DISTINCT vendor_id) AS vendors FROM leakage_findings GROUP BY kind ORDER BY amount DESC").fetchall()
        top = c.execute("""SELECT f.kind, v.canonical_name AS vendor, f.detail, f.expected_amount, f.actual_amount, f.variance, f.confidence, t.citation_anchor, t.quote, k.title AS contract, i.invoice_number, i.invoice_date
                             FROM leakage_findings f JOIN vendors v ON v.id = f.vendor_id JOIN contract_terms t ON t.id = f.contract_term_id JOIN contracts k ON k.id = t.contract_id
                             LEFT JOIN invoice_lines l ON l.id = f.invoice_line_id LEFT JOIN invoices i ON i.id = l.invoice_id ORDER BY f.variance DESC LIMIT %s""", [max(1, min(limit, 100))]).fetchall()
        by_vendor = c.execute("""SELECT v.id AS vendor_id, v.canonical_name AS vendor, count(*) AS findings, round(sum(f.variance), 2) AS amount FROM leakage_findings f JOIN vendors v ON v.id = f.vendor_id
                                  WHERE f.kind = 'rate_above_contract' GROUP BY v.id ORDER BY amount DESC LIMIT 5""").fetchall()
        dup, _ = duplicate_pairs(c, ctx)
        spend = c.execute("SELECT coalesce(sum(total) FILTER (WHERE total > 0), 0) AS s, min(invoice_date) AS a, max(invoice_date) AS b FROM invoices").fetchone()
    total = sum(float(k["amount"]) for k in kinds) + sum(d["amount"] for d in dup)
    months = max(1, round(((spend["b"] - spend["a"]).days + 1) / 30.4)) if spend["a"] else 12
    return jsonable_encoder({"period_months": months, "spend": round(float(spend["s"]), 2), "by_kind": kinds + [{"kind": "duplicate_payment", "findings": len(dup), "amount": round(sum(d["amount"] for d in dup), 2), "vendors": None}],
                             "total": round(total, 2), "annualized": round(total * 12 / months, 2), "share_of_spend": round(total / float(spend["s"]), 5) if spend["s"] else 0,
                             "top_rate_vendors": by_vendor, "top_findings": top})


# ---------------------------------------------------------------- opportunity-engine + supplier-intelligence

@app.get("/v1/opportunities", tags=["opportunities"], summary="Ranked savings opportunities (recomputed and stored on each call)")
def opportunities(ctx: Ctx = Depends(auth("spend:read"))):
    with db.tx(ctx.tenant_id) as c:
        today = datetime.date.today()
        opps = []
        dup, _ = duplicate_pairs(c, ctx)
        if dup:
            vend = {r["id"]: r["vendor_id"] for r in c.execute("SELECT id, vendor_id FROM invoices WHERE id = ANY(%s)", [[d["duplicate"] for d in dup]])}
            opps.append({"type": "recover_duplicate_payments", "title": f"Recover {len(dup)} duplicate payments", "vendors": sorted({str(v) for v in vend.values() if v})[:50], "savings": sum(d["amount"] for d in dup),
                         "effort": "low", "confidence": 0.9, "months": None, "evidence": {"pairs": len(dup), "entered_under_another_spelling": sum(d["cross_spelling"] for d in dup)}})
        for r in c.execute("""SELECT f.kind, f.vendor_id, v.canonical_name, count(*) AS n, sum(f.variance) AS amt, min(k.end_date) AS renew FROM leakage_findings f JOIN vendors v ON v.id = f.vendor_id
                                JOIN contract_terms t ON t.id = f.contract_term_id JOIN contracts k ON k.id = t.contract_id GROUP BY f.kind, f.vendor_id, v.canonical_name"""):
            rate = r["kind"] == "rate_above_contract"
            opps.append({"type": "recover_overbilling" if rate else "claim_missed_rebate", "title": f"{'Recover billing above the rate card from' if rate else 'Claim the unpaid volume rebate from'} {r['canonical_name']}",
                         "vendors": [str(r["vendor_id"])], "savings": float(r["amt"]), "effort": "low", "confidence": 0.95 if rate else 0.85, "months": (r["renew"] - today).days / 30.4,
                         "evidence": {"findings": r["n"], "contract_ends": r["renew"].isoformat()}})
        for r in c.execute("""SELECT c.name AS category, array_agg(x.vendor_id ORDER BY x.amt DESC) AS vendors, array_agg(x.canonical_name ORDER BY x.amt DESC) AS names, array_agg(x.amt ORDER BY x.amt DESC) AS amts
                                FROM (SELECT l.category_id, i.vendor_id, v.canonical_name, sum(l.amount) AS amt FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id JOIN vendors v ON v.id = i.vendor_id
                                       WHERE l.amount > 0 GROUP BY 1, 2, 3) x JOIN spend_categories c ON c.id = x.category_id JOIN spend_categories p ON p.id = c.parent_id
                               WHERE p.name = 'IT' AND c.name LIKE '%%software' GROUP BY c.name HAVING count(*) BETWEEN 4 AND 8"""):
            moved = sum(float(a) for a in r["amts"][2:])                      # keep the two largest, move the rest
            opps.append({"type": "consolidate_vendors", "title": f"Consolidate {len(r['vendors'])} overlapping {r['category'].lower()} vendors to two", "vendors": [str(v) for v in r["vendors"]],
                         "savings": moved * CONSOLIDATION_RATE, "effort": "high", "confidence": 0.5, "months": None,
                         "evidence": {"category": r["category"], "vendors": [{"name": n, "spend": round(float(a), 2)} for n, a in zip(r["names"], r["amts"])], "spend_moved": round(moved, 2),
                                      "assumed_saving_rate": CONSOLIDATION_RATE}})
        c.execute("DELETE FROM opportunities")
        for o in opps:
            o["score"] = engine.score(o["savings"], o["confidence"], o["effort"], o["months"])
            c.execute("INSERT INTO opportunities (id, tenant_id, opportunity_type, vendor_ids, estimated_savings, effort, evidence, status, score, title) VALUES (%s,%s,%s,%s,%s,%s,%s,'open',%s,%s)",
                      [uuid.uuid7(), ctx.tenant_id, o["type"], o["vendors"], round(o["savings"], 2), o["effort"], Jsonb(o["evidence"]), o["score"], o["title"]])
        rows = c.execute("SELECT id, opportunity_type, title, estimated_savings, effort, score, evidence, vendor_ids FROM opportunities ORDER BY score DESC").fetchall()
        pipeline = c.execute("SELECT opportunity_type, count(*) AS n, round(sum(estimated_savings), 2) AS savings FROM opportunities GROUP BY 1 ORDER BY savings DESC").fetchall()
    return jsonable_encoder({"count": len(rows), "pipeline_total": round(sum(float(r["estimated_savings"]) for r in rows), 2), "pipeline": pipeline, "scoring": "savings x confidence x renewal timing / effort",
                             "consolidation": next((r for r in rows if r["opportunity_type"] == "consolidate_vendors"), None), "items": rows[:10]})


@app.get("/v1/vendors/{vendor_id}/dossier", tags=["vendors"], summary="Negotiation dossier: spend, contract terms, findings and alternatives")
def dossier(vendor_id: uuid.UUID, ctx: Ctx = Depends(auth("spend:read"))):
    with db.tx(ctx.tenant_id) as c:
        v = c.execute("SELECT id, canonical_name FROM vendors WHERE id = %s", [vendor_id]).fetchone()
        if not v:
            raise Problem(404, "vendor_not_found")
        aliases = c.execute("SELECT raw_name, source_system, match_confidence FROM vendor_aliases WHERE vendor_id = %s ORDER BY raw_name", [vendor_id]).fetchall()
        q = """SELECT {} AS k, round(sum(l.amount), 2) AS amount FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id LEFT JOIN spend_categories c ON c.id = l.category_id WHERE i.vendor_id = %s GROUP BY 1 ORDER BY {}"""
        by_q = c.execute(q.format("to_char(i.invoice_date, 'YYYY-\"Q\"Q')", "1"), [vendor_id]).fetchall()
        by_cat = c.execute(q.format("coalesce(c.name, 'Uncategorized')", "2 DESC"), [vendor_id]).fetchall()
        by_bu = c.execute(q.format("i.business_unit", "2 DESC"), [vendor_id]).fetchall()
        terms = c.execute("SELECT t.term_type, t.key, t.value_json AS value, t.citation_anchor, k.title, k.end_date FROM contract_terms t JOIN contracts k ON k.id = t.contract_id WHERE k.vendor_id = %s ORDER BY t.term_type, t.key", [vendor_id]).fetchall()
        leak = c.execute("SELECT kind, count(*) AS findings, round(sum(variance), 2) AS amount FROM leakage_findings WHERE vendor_id = %s GROUP BY kind", [vendor_id]).fetchall()
        roles = c.execute("""SELECT t.key AS role, t.value_json AS contract_rate, round(avg(l.unit_price), 2) AS avg_billed, round(max(l.unit_price), 2) AS max_billed, round(sum(l.qty)) AS hours
                               FROM contract_terms t JOIN contracts k ON k.id = t.contract_id JOIN invoices i ON i.vendor_id = k.vendor_id JOIN invoice_lines l ON l.invoice_id = i.id AND l.description ~ ('^(Contractor )?' || t.key || ' (-|week)')
                              WHERE k.vendor_id = %s AND t.term_type = 'rate_card' GROUP BY t.key, t.value_json ORDER BY hours DESC""", [vendor_id]).fetchall()
        alts = c.execute("""SELECT v.canonical_name AS vendor, round(sum(l.amount), 2) AS spend FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id JOIN vendors v ON v.id = i.vendor_id
                             WHERE i.vendor_id <> %s AND l.category_id = (SELECT l2.category_id FROM invoice_lines l2 JOIN invoices i2 ON i2.id = l2.invoice_id WHERE i2.vendor_id = %s AND l2.category_id IS NOT NULL
                                                                           GROUP BY 1 ORDER BY sum(l2.amount) DESC LIMIT 1) GROUP BY 1 ORDER BY 2 DESC LIMIT 5""", [vendor_id, vendor_id]).fetchall()
    spend = sum(float(r["amount"]) for r in by_q)
    end = next((t["end_date"] for t in terms), None)
    points = [f"${sum(float(x['amount']) for x in leak):,.0f} of findings to recover before any renewal conversation" if leak else None,
              f"contract ends {end}: {(end - datetime.date.today()).days} days to negotiate" if end else "no contract on file: spend is off-contract",
              f"{len(alts)} other vendors already supply this category" if alts else None, f"{len(aliases)} vendor records were consolidated into this view" if len(aliases) > 1 else None]
    return jsonable_encoder({"vendor_id": vendor_id, "name": v["canonical_name"], "aliases": aliases, "spend": round(spend, 2), "spend_by_quarter": by_q, "spend_by_category": by_cat, "spend_by_business_unit": by_bu,
                             "contract_terms": terms, "rate_card_vs_billed": roles, "leakage": leak, "alternatives": alts, "talking_points": [p for p in points if p]})
