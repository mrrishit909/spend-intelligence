"""Deterministic synthetic accounts-payable world: a messy vendor master, three years of invoices, contracts, and seeded leakage.

Every company, invoice and contract is invented. `build()` is pure, so the seed, the import job, the evaluation and the
tests share one world. Year index 2 is the most recent twelve months (the one the demo imports).
"""
import datetime
import random
import uuid

# category -> (parent, description phrases, typical line amount, vendor name endings)
CATEGORIES = {
    "Cloud infrastructure": ("IT", ["compute instance hours", "object storage GB-month", "data transfer out", "managed database", "reserved capacity", "kubernetes cluster fee"], 9000, ["Cloud Services", "Hosting", "Data Systems"]),
    "Collaboration software": ("IT", ["team chat seats annual", "video meeting licences", "document workspace subscription", "project board seats", "whiteboard plan", "shared calendar suite"], 3200, ["Collab", "Workspace", "Teams Software", "Meetings"]),
    "CRM software": ("IT", ["sales cloud seats", "CRM platform subscription", "marketing automation tier", "customer data platform"], 6000, ["CRM", "Customer Cloud"]),
    "Hardware": ("IT", ["laptop 14in", "docking station", "monitor 27in", "server chassis", "network switch 48p"], 2400, ["Computers", "Devices", "Hardware"]),
    "Telecom": ("IT", ["mobile plan lines", "SIP trunk usage", "dedicated internet access", "conference bridge minutes"], 1500, ["Telecom", "Networks", "Wireless"]),
    "Consulting": ("Professional services", ["{role} - {hrs} hrs @ {rate}"], 0, ["Consulting", "Advisory", "Partners"]),
    "Legal": ("Professional services", ["{role} - {hrs} hrs @ {rate}"], 0, ["Law", "Legal", "LLP"]),
    "Staffing": ("Professional services", ["Contractor {role} week ending {hrs} hrs @ {rate}"], 0, ["Staffing", "Talent", "Resourcing"]),
    "Rent": ("Facilities", ["office lease monthly rent", "common area maintenance", "parking allocation"], 22000, ["Properties", "Realty"]),
    "Utilities": ("Facilities", ["electricity charges", "natural gas supply", "water and sewer", "waste collection"], 3800, ["Energy", "Utilities", "Power"]),
    "Cleaning": ("Facilities", ["janitorial service monthly", "window cleaning", "carpet deep clean", "restroom supplies"], 1900, ["Cleaning Services", "Janitorial"]),
    "Security": ("Facilities", ["guard service hours", "alarm monitoring", "badge system maintenance"], 2600, ["Security", "Protection"]),
    "Advertising": ("Marketing", ["paid search spend", "display campaign", "social ads management", "billboard placement"], 7000, ["Media", "Advertising"]),
    "Events": ("Marketing", ["trade show booth", "event catering", "venue hire", "conference sponsorship"], 5200, ["Events", "Exhibitions"]),
    "Agencies": ("Marketing", ["creative retainer", "brand design sprint", "PR retainer monthly", "copywriting package"], 6400, ["Creative", "Agency", "Studio"]),
    "Air travel": ("Travel", ["airfare economy", "airfare change fee", "seat upgrade", "baggage fee"], 620, ["Airways", "Air"]),
    "Hotels": ("Travel", ["hotel nights", "room tax", "conference room rental"], 480, ["Hotels", "Suites", "Inns"]),
    "Ground transport": ("Travel", ["car rental days", "rideshare trips", "airport transfer", "rail ticket"], 140, ["Car Rental", "Mobility"]),
    "Freight": ("Logistics", ["LTL freight shipment", "full truckload linehaul", "fuel surcharge", "liftgate fee"], 2100, ["Freight", "Logistics", "Trucking"]),
    "Courier": ("Logistics", ["overnight parcel", "ground parcel", "international express", "signature required fee"], 60, ["Express", "Courier"]),
    "Office supplies": ("Office", ["copy paper case", "toner cartridge", "pens box", "sticky notes", "coffee supplies"], 110, ["Office Supply", "Stationery"]),
}
ROLES = {"Consulting": {"Partner": 420, "Senior Consultant": 185, "Consultant": 140, "Analyst": 95}, "Legal": {"Partner": 650, "Associate": 380, "Paralegal": 145},
         "Staffing": {"Software Engineer": 92, "Data Analyst": 71, "Project Coordinator": 48, "Help Desk Technician": 39}}
WORDS = ("Nimbus Stratus Cirrus Alder Kestrel Tidewater Meridian Juniper Quarry Lantern Foundry Orchard Summit Cedar Beacon Ironwood Marlow Pinnacle Riverside Halcyon Copperline Northgate "
         "Harbor Granite Willow Falcon Ember Cobalt Drift Aster Linden Onyx Sable Thistle Vireo Wren Zephyr Bramble Corvid Dune Elmwood Fathom").split()
SUFFIX = ["Inc", "LLC", "Ltd", "Corp", "Co"]
ABBR = {"Services": "Svcs", "Systems": "Sys", "International": "Intl", "Software": "Sftwr", "Consulting": "Cnsltg", "Properties": "Props", "Logistics": "Logs", "Advertising": "Advt", "Security": "Sec"}
UNITS = ["Operations", "Sales", "Engineering", "Marketing", "Finance", "Customer Support"]
RATE_VENDORS = ("Consulting", "Legal", "Staffing")
REBATE_CATEGORIES = ("Cloud infrastructure", "Collaboration software", "CRM software", "Telecom", "Freight")


def alias_name(rng, name, suffix):
    """One way a source system might have typed this vendor's name."""
    n = name
    r = rng.random()
    if r < 0.3:
        n = " ".join(ABBR.get(w, w) for w in n.split())
    if rng.random() < 0.35:
        n = n.upper()
    tail = rng.choice([f" {suffix}", f", {suffix}.", f" {suffix}.", "", {"Inc": " Incorporated", "Corp": " Corporation", "Ltd": " Limited", "Co": " Company", "LLC": " L.L.C."}[suffix]])
    if rng.random() < 0.12 and len(n) > 8:                      # a typo
        i = rng.randrange(2, len(n) - 2)
        n = n[:i] + n[i + 1:]
    if rng.random() < 0.1:
        n = n.replace(" ", "  ", 1)
    return (n + (tail.upper() if n.isupper() else tail)).strip()


def contract_text(rng, vendor, category, rates, rebate, start, end, net, cap):
    """A services or subscription agreement in plain text. Section numbers are the citation anchors."""
    v = rng.randrange(3)
    s = [f"MASTER AGREEMENT between Acme Holdings (\"Customer\") and {vendor} (\"Supplier\")", "",
         f"1. TERM. {['This Agreement commences on', 'The term begins', 'Effective from'][v]} {start.isoformat()} {['and continues until', 'and ends on', 'through'][v]} {end.isoformat()}."
         f" {['It renews automatically for successive one-year terms unless either party gives 60 days written notice.', 'Renewal is automatic absent 60 days notice.', 'Either party may decline renewal with 60 days notice.'][v]}",
         f"2. PAYMENT. {['Invoices are payable net', 'Payment terms are Net', 'Customer shall pay within'][v]} {net} {['days of receipt.', '.', 'days.'][v]}"]
    if rates:
        s.append("3. RATE CARD. " + ["Supplier shall charge the following hourly rates:", "Services are billed per hour as follows:", "The agreed hourly fees are:"][v])
        for role, rate in rates.items():
            s.append("   " + [f"{role}: ${rate:.2f} per hour", f"{role} - USD {rate:.2f}/hour", f"{role}, {rate:.2f} dollars per hour"][v])
    if rebate:
        thr, pct = rebate
        s.append("4. VOLUME REBATE. " + [f"If Customer's fees in a contract year exceed ${thr:,.0f}, Supplier shall credit a rebate of {pct}% of that year's fees.",
                                          f"A {pct}% rebate applies to annual spend above ${thr:,.0f}.",
                                          f"Where annual fees are greater than ${thr:,.0f}, Supplier will issue a credit equal to {pct}% of annual fees."][v])
    s.append(f"5. PRICE CHANGES. {['Rates may increase no more than', 'Annual price increases are capped at', 'Any uplift shall not exceed'][v]} {cap}% {['per contract year.', '.', 'in any twelve-month period.'][v]}")
    s.append("6. GOVERNING LAW. This Agreement is governed by the laws of the State of Delaware.")
    return "\n".join(s)


def build(scale=1.0, seed=29, today=None):
    rng = random.Random(seed)
    today = today or datetime.datetime.now(datetime.UTC).date()
    year_end = today.replace(day=1)
    uid = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)      # noqa: E731
    cats = list(CATEGORIES)
    vendors, aliases = [], []
    n_vendors = max(60, int(420 * scale))
    per_cat = {c: max(2, n_vendors // len(cats)) for c in cats}
    per_cat["Collaboration software"] = 6                                # six tools that do much the same thing
    used = set()
    for cat in cats:
        for k in range(per_cat[cat]):
            while True:
                name = f"{rng.choice(WORDS)} {rng.choice(CATEGORIES[cat][3])}"
                if name not in used:
                    used.add(name)
                    break
            v = {"id": uid(), "name": name, "suffix": rng.choice(SUFFIX), "category": cat, "country": rng.choice(["US", "US", "US", "CA", "GB", "DE"]), "tax_id": f"{rng.randrange(10, 99)}-{rng.randrange(10**6, 10**7)}",
                 "bank": f"BK{rng.randrange(10**9):09d}", "address": f"{rng.randrange(1, 9999)} {rng.choice(WORDS)} Street", "weight": rng.lognormvariate(0, 0.9), "aliases": []}
            for j in range(rng.choice([1, 2, 2, 3, 4]) if cat != "Cloud infrastructure" or k else 4):       # the first cloud vendor has exactly four spellings
                a = {"id": uid(), "vendor": len(vendors), "source_system": ["erp", "ap", "card", "erp"][j % 4], "source_vendor_id": f"V{len(aliases):06d}", "raw_name": alias_name(rng, name, v["suffix"]) if j else f"{name} {v['suffix']}",
                     "tax_id": v["tax_id"] if rng.random() < 0.7 else None, "bank": v["bank"] if rng.random() < 0.6 else None,
                     "address": v["address"].upper().replace("STREET", "ST") if rng.random() < 0.3 else v["address"] if rng.random() < 0.8 else None}
                v["aliases"].append(len(aliases))
                aliases.append(a)
            vendors.append(v)
    # ---- contracts for rate-card and rebate vendors ----------------------------------------------------------
    contracts = []
    for i, v in enumerate(vendors):
        cat = v["category"]
        rates = {r: round(base * rng.uniform(0.9, 1.15), 2) for r, base in ROLES[cat].items()} if cat in RATE_VENDORS else None
        rebate = (round(rng.choice([150_000, 250_000, 400_000]) * max(0.05, scale)), rng.choice([3, 4, 5])) if cat in REBATE_CATEGORIES and rng.random() < 0.7 else None
        if rates or rebate:
            start = year_end.replace(year=year_end.year - 3)
            end = year_end + datetime.timedelta(days=rng.randrange(30, 500))
            net, cap = rng.choice([30, 45, 60]), rng.choice([2, 3, 5])
            v["contract"] = len(contracts)
            contracts.append({"id": uid(), "vendor": i, "title": f"Master agreement: {v['name']}", "start": start, "end": end, "rates": rates, "rebate": rebate, "net_days": net, "price_cap_pct": cap,
                              "text": contract_text(rng, f"{v['name']} {v['suffix']}", cat, rates, rebate, start, end, net, cap)})
    # ---- invoices: three years, month by month ----------------------------------------------------------------
    invoices, lines, pos = [], [], []
    months = [(year_end.year - 3 + (year_end.month - 1 + m) // 12, (year_end.month - 1 + m) % 12 + 1) for m in range(36)]
    per_month = max(40, int(3400 * scale))
    weights = [v["weight"] for v in vendors]
    seq = [rng.randrange(100, 9000) for _ in vendors]
    fmt = [rng.choice(["INV-{:05d}", "{:06d}", "SI{:05d}", "INV{:05d}", "{}"]) for _ in vendors]
    for m, (y, mo) in enumerate(months):
        for vi in rng.choices(range(len(vendors)), weights, k=per_month):
            v, cat = vendors[vi], vendors[vi]["category"]
            a = rng.choice(v["aliases"])
            seq[vi] += 1
            date = datetime.date(y, mo, rng.randrange(1, 28))
            inv = {"id": uid(), "alias": a, "vendor": vi, "number": fmt[vi].format(seq[vi]), "date": date, "due": date + datetime.timedelta(days=30), "unit": rng.choice(UNITS), "year": m // 12, "lines": [],
                   "po": None, "dup_of": None}
            if rng.random() < 0.6:
                inv["po"] = len(pos)
                pos.append({"id": uid(), "vendor": vi, "number": f"PO-{len(pos):06d}", "date": date - datetime.timedelta(days=rng.randrange(3, 40)), "category": cat})
            for _ in range(rng.choice([1, 1, 2, 3, 4, 6])):
                if cat in RATE_VENDORS:
                    role = rng.choice(list(ROLES[cat]))
                    agreed = contracts[v["contract"]]["rates"][role]
                    over = rng.random() < 0.06                              # billed above the rate card
                    rate = round(agreed * rng.uniform(1.08, 1.25), 2) if over else agreed
                    hrs = rng.choice([8, 16, 24, 40, 40, 80, 120])
                    desc = CATEGORIES[cat][1][0].format(role=role, hrs=hrs, rate=f"{rate:.2f}")
                    ln = {"qty": hrs, "unit_price": rate, "overbilled": round((rate - agreed) * hrs, 2) if over else 0.0, "role": role}
                else:
                    phrase = rng.choice(CATEGORIES[cat][1])
                    qty = rng.choice([1, 1, 1, 2, 5, 10])
                    price = round(CATEGORIES[cat][2] * rng.lognormvariate(-0.2, 0.6) / qty, 2)
                    desc = phrase if rng.random() < 0.7 else f"{phrase} {rng.choice(['Q', 'ref ', '#', 'PO '])}{rng.randrange(1000, 9999)}"
                    if rng.random() < 0.15:
                        desc = desc.upper()
                    ln = {"qty": qty, "unit_price": price, "overbilled": 0.0, "role": None}
                ln.update(id=uid(), invoice=len(invoices), description=desc, amount=round(ln["qty"] * ln["unit_price"], 2), category=cat, source_category=cat if rng.random() < 0.75 else None)
                inv["lines"].append(len(lines))
                lines.append(ln)
            inv["total"] = round(sum(lines[i]["amount"] for i in inv["lines"]), 2)
            invoices.append(inv)
            if rng.random() < 0.005:                                        # paid twice: same goods, a slightly different invoice number, often entered under another spelling
                others = [x for x in v["aliases"] if x != a]
                dup = {**inv, "id": uid(), "alias": rng.choice(others) if others and rng.random() < 0.6 else a, "date": date + datetime.timedelta(days=rng.randrange(1, 12)), "lines": [], "dup_of": len(invoices) - 1}
                n = inv["number"]
                dup["number"] = rng.choice([n.replace("INV-", ""), n + "-A", n.replace("0", "O", 1), n.lstrip("0"), f"{n} "])
                dup["due"] = dup["date"] + datetime.timedelta(days=30)
                for i in inv["lines"]:
                    lines.append({**lines[i], "id": uid(), "invoice": len(invoices)})
                    dup["lines"].append(len(lines) - 1)
                invoices.append(dup)
    # ---- rebates: owed when a contract year's spend passes the threshold; credited only some of the time ---------
    rebates = []
    for ci, c in enumerate(contracts):
        if not c["rebate"]:
            continue
        thr, pct = c["rebate"]
        for yr in range(3):
            spend = sum(inv["total"] for inv in invoices if inv["vendor"] == c["vendor"] and inv["year"] == yr and inv["dup_of"] is None)
            if spend > thr:
                credited = rng.random() < 0.45
                rebates.append({"contract": ci, "vendor": c["vendor"], "year": yr, "spend": round(spend, 2), "owed": round(spend * pct / 100, 2), "credited": credited})
                if credited:                                                 # a credit note line on a year-end invoice
                    y, mo = months[yr * 12 + 11]
                    v = vendors[c["vendor"]]
                    inv = {"id": uid(), "alias": v["aliases"][0], "vendor": c["vendor"], "number": f"CN-{yr}{ci:04d}", "date": datetime.date(y, mo, 27), "due": datetime.date(y, mo, 27), "unit": "Finance", "year": yr,
                           "lines": [len(lines)], "po": None, "dup_of": None, "total": -round(spend * pct / 100, 2)}
                    lines.append({"id": uid(), "invoice": len(invoices), "description": f"volume rebate credit {pct}% contract year", "qty": 1, "unit_price": inv["total"], "amount": inv["total"], "category": v["category"],
                                  "source_category": v["category"], "overbilled": 0.0, "role": None})
                    invoices.append(inv)
    return {"today": today, "months": months, "vendors": vendors, "aliases": aliases, "contracts": contracts, "invoices": invoices, "lines": lines, "pos": pos, "rebates": rebates, "categories": cats}
