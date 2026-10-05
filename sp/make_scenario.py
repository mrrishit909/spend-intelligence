"""Write web/scenario.json. Vendor names in the demo come from the deterministic world, so they are looked up here.

    python -m sp.make_scenario
"""
import json

from core import db

from . import world
from .seed import TENANTS


def main():
    w = world.build(TENANTS["acme"][1], TENANTS["acme"][2])
    cloud = next(v for v in w["vendors"] if v["category"] == "Cloud infrastructure")
    over = {}
    for ln in w["lines"]:
        inv = w["invoices"][ln["invoice"]]
        if inv["year"] == 2 and ln["overbilled"]:
            over[inv["vendor"]] = over.get(inv["vendor"], 0) + ln["overbilled"]
    firm = w["vendors"][max((v for v in over if w["vendors"][v]["category"] == "Consulting"), key=over.get)]
    C = lambda key, who, method, path, body=None: {"key": key, "as": who, "method": method, "path": path, **({"body": body} if body is not None else {})}      # noqa: E731
    q = lambda name: "/v1/vendors?q=" + name.replace(" ", "+")      # noqa: E731
    s = {"service": "spend-intelligence", "run": "demo-1", "tokens": {"procurement": "acme-procurement-demo", "analyst": "acme-analyst-demo", "admin": "acme-admin-demo"},
         "steps": [
             {"title": "Import one year of AP data", "say": "A year of invoices arrives from three source systems (ERP, accounts payable, purchasing cards), each with its own vendor list. Nothing has been cleaned yet.",
              "calls": [C("imp", "procurement", "POST", "/v1/imports", {"source": "erp-extract", "fiscal_year": "latest"})]},
             {"title": "Merge four spellings into one vendor", "say": "The same supplier was typed differently in each system. Entity resolution compares names, tax ids, bank accounts and addresses and decides which records are one company.",
              "calls": [C("res", "procurement", "POST", "/v1/vendors:resolve", {}), C("cloudv", "analyst", "GET", q(cloud["name"])), C("cloud", "analyst", "GET", "/v1/vendors/{{cloudv.items.0.id}}/dossier")]},
             {"title": "Classify uncategorized spend", "say": "A quarter of invoice lines came with no category. A classifier reads each line's description and files it in the spend taxonomy, with a confidence.",
              "calls": [C("cls", "procurement", "POST", "/v1/spend:classify"), C("cube", "analyst", "GET", "/v1/spend/cube?rows=parent_category&cols=quarter")]},
             {"title": "A duplicate invoice, and an hourly rate above contract", "say": "Duplicates hide behind a changed invoice number and a different spelling of the vendor. Separately, a consulting firm's contract is read for its rate card, with the clause each rate came from.",
              "calls": [C("dup", "analyst", "GET", "/v1/invoices/duplicates?limit=5"), C("firm", "analyst", "GET", q(firm["name"])), C("ks", "analyst", "GET", "/v1/contracts?vendor_id={{firm.items.0.id}}"),
                        C("ext", "procurement", "POST", "/v1/contracts/{{ks.items.0.id}}:extract"), C("scan", "procurement", "POST", "/v1/leakage:scan")]},
             {"title": "Annualized leakage", "say": "Every contract is read and every invoice line checked against it: hours billed above the rate card, volume rebates earned but never credited, and payments made twice.",
              "calls": [C("leak", "analyst", "GET", "/v1/leakage")]},
             {"title": "Six overlapping software vendors", "say": "Six suppliers sell much the same collaboration software to different teams. Moving the smaller four onto the two largest is a consolidation opportunity, ranked alongside the money simply owed back.",
              "calls": [C("opp", "analyst", "GET", "/v1/opportunities")]},
             {"title": "Negotiation dossier and the savings pipeline", "say": "Everything known about the consulting firm on one page for the renewal conversation, and the pipeline total for the CFO.",
              "calls": [C("dos", "analyst", "GET", "/v1/vendors/{{firm.items.0.id}}/dossier"), C("audit", "admin", "GET", "/v1/audit?limit=8")]}]}
    (db.ROOT / "web" / "scenario.json").write_text(json.dumps(s, indent=1))
    print("cloud vendor:", cloud["name"], "| consulting firm:", firm["name"])


if __name__ == "__main__":
    main()
