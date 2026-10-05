"""The analytical core, as pure functions: vendor-resolution, taxonomy classifier, invoice-matcher (duplicates),
contract-extractor, leakage-engine and the opportunity scorer.
"""
import re
from collections import defaultdict

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

VERSION = "spend-1.0.0"
LEGAL = {"inc", "incorporated", "llc", "l", "c", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "plc"}
EXPAND = {"svcs": "services", "sys": "systems", "intl": "international", "sftwr": "software", "cnsltg": "consulting", "props": "properties", "logs": "logistics", "advt": "advertising", "sec": "security", "and": "&"}
MERGE_AT = 0.5


# ---------------------------------------------------------------- vendor-resolution

def normalize(name):
    """Lower-case, strip punctuation and legal endings, expand common abbreviations."""
    toks = [EXPAND.get(t, t) for t in re.findall(r"[a-z0-9&]+", name.lower())]
    toks = [t for t in toks if t not in LEGAL]
    return " ".join(toks)


def _trigrams(s):
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def pair_features(a, b):
    """Evidence that two vendor records are the same company. Missing identifiers are neutral (0), conflicts are negative."""
    na, nb = a["norm"], b["norm"]
    ta, tb = set(na.split()), set(nb.split())
    ga, gb = _trigrams(na), _trigrams(nb)
    agree = lambda k: 0.0 if not a.get(k) or not b.get(k) else 1.0 if a[k] == b[k] else -1.0      # noqa: E731
    addr = 0.0
    if a.get("address") and b.get("address"):
        x, y = set(re.findall(r"[a-z0-9]+", a["address"].lower().replace("street", "st"))), set(re.findall(r"[a-z0-9]+", b["address"].lower().replace("street", "st")))
        addr = 1.0 if x == y else -0.5
    return [len(ta & tb) / max(1, len(ta | tb)), len(ga & gb) / max(1, len(ga | gb)), agree("tax_id"), agree("bank"), addr]


def candidate_pairs(records):
    """Blocking: only records that share a name token, a tax id or a bank account are compared."""
    buckets = defaultdict(list)
    for i, r in enumerate(records):
        for t in set(r["norm"].split()):
            buckets[("t", t)].append(i)
        for k in ("tax_id", "bank"):
            if r.get(k):
                buckets[(k, r[k])].append(i)
    pairs = set()
    for members in buckets.values():
        if len(members) <= 60:               # ponytail: a token shared by more than 60 records ("services") is too common to block on
            pairs.update((members[a], members[b]) for a in range(len(members)) for b in range(a + 1, len(members)))
    return sorted(pairs)


def prepare(records):
    return [{**r, "norm": normalize(r["raw_name"])} for r in records]


def train_resolver(records, truth):
    recs = prepare(records)
    pairs = candidate_pairs(recs)
    x = np.array([pair_features(recs[i], recs[j]) for i, j in pairs])
    y = np.array([truth[i] == truth[j] for i, j in pairs], int)
    return LogisticRegression(C=5, max_iter=500).fit(x, y)


def resolve(records, model=None, decisions=None):
    """-> (cluster index per record, {pair: probability}). `decisions` are human overrides: {(i, j): True/False}.
    model=None is the baseline: merge only records whose normalized names are identical."""
    recs = prepare(records)
    parent = list(range(len(recs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs, proba = candidate_pairs(recs), {}
    if pairs and model is not None:
        p = model.predict_proba(np.array([pair_features(recs[i], recs[j]) for i, j in pairs]))[:, 1]
        proba = {pr: float(v) for pr, v in zip(pairs, p)}
    else:
        proba = {(i, j): 1.0 if recs[i]["norm"] == recs[j]["norm"] else 0.0 for i, j in pairs}
    for pr, ok in (decisions or {}).items():
        proba[tuple(sorted(pr))] = 1.0 if ok else 0.0
    for (i, j), v in proba.items():
        if v >= MERGE_AT:
            parent[find(i)] = find(j)
    roots = {}
    return [roots.setdefault(find(i), len(roots)) for i in range(len(recs))], proba


def pair_scores(pred, truth):
    def pairs(keys):
        c = defaultdict(int)
        for k in keys:
            c[k] += 1
        return sum(n * (n - 1) // 2 for n in c.values())
    both, p, t = pairs(zip(pred, truth)), pairs(pred), pairs(truth)
    prec, rec = both / max(1, p), both / max(1, t)
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(2 * prec * rec / max(1e-9, prec + rec), 4)}


# ---------------------------------------------------------------- taxonomy-service

RULES = [(r"\b(hrs|hours?) @", None), (r"rebate credit", None)]          # shapes handled elsewhere; kept out of the keyword baseline
KEYWORDS = {"Cloud infrastructure": "compute|storage|kubernetes|database", "Collaboration software": "chat|meeting|workspace|whiteboard", "CRM software": "crm|sales cloud",
            "Hardware": "laptop|monitor|server|switch", "Telecom": "mobile|sip|internet", "Rent": "rent|lease", "Utilities": "electricity|gas|water", "Cleaning": "janitorial|cleaning|clean",
            "Security": "guard|alarm|badge", "Advertising": "search|campaign|ads|billboard", "Events": "booth|catering|venue", "Agencies": "retainer|design|copywriting",
            "Air travel": "airfare|baggage", "Hotels": "hotel|room", "Ground transport": "rental|rideshare|rail", "Freight": "freight|truckload|fuel", "Courier": "parcel|express",
            "Office supplies": "paper|toner|pens|coffee", "Consulting": "consultant|analyst", "Legal": "associate|paralegal", "Staffing": "contractor"}


def keyword_category(text):
    low = text.lower()
    return next((c for c, pat in KEYWORDS.items() if re.search(pat, low)), "Office supplies")


def train_classifier(texts, labels):
    return make_pipeline(TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True, min_df=2), LogisticRegression(C=8, max_iter=300)).fit(texts, labels)


# ---------------------------------------------------------------- invoice-matcher: duplicates

def number_key(number):
    """The digits of an invoice number with look-alike letters fixed and leading zeros dropped: 'INV-00123', '123', '00123-A' -> '123'."""
    digits = re.sub(r"\D", "", number.upper().replace("O", "0"))
    return digits.lstrip("0") or "0"


def duplicates(invoices, window_days=14, by="vendor_id"):
    """Probable duplicate pairs: same vendor, same total, close dates, and invoice numbers that reduce to the same digits.
    invoices: [{id, vendor_id, alias_id, number, date, total}]. by="alias_id" with exact numbers is the baseline most AP systems apply."""
    groups = defaultdict(list)
    for inv in invoices:
        if inv["total"] > 0:
            groups[(inv[by], round(inv["total"] * 100))].append(inv)
    out = []
    for members in groups.values():
        members.sort(key=lambda x: x["date"])
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                x, y = members[a], members[b]
                if (y["date"] - x["date"]).days > window_days:
                    break
                same = x["number"].strip() == y["number"].strip() if by == "alias_id" else number_key(x["number"]) == number_key(y["number"])
                if same:
                    out.append({"original": x["id"], "duplicate": y["id"], "amount": y["total"], "days_apart": (y["date"] - x["date"]).days,
                                "confidence": 0.99 if x["number"].strip() == y["number"].strip() else 0.9, "cross_spelling": x["alias_id"] != y["alias_id"],
                                "evidence": f"same vendor and total; numbers '{x['number'].strip()}' and '{y['number'].strip()}' both reduce to {number_key(x['number'])}"})
    return out


# ---------------------------------------------------------------- contract-extractor

MONEY = r"\$?\s?(?:USD\s)?([\d,]+(?:\.\d+)?)"


def extract_terms(text):
    """Commercial terms with the section they were read from and the exact sentence as evidence. Regex over typed text:
    the same shape a language-model extractor would return, but with nothing to hallucinate."""
    terms = []
    section = None
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\.\s+([A-Z ]+)\.", line)
        if m:
            section = f"§{m.group(1)} {m.group(2).title().strip()}"
        s = line.strip()
        add = lambda t, k, v: terms.append({"term_type": t, "key": k, "value": v, "citation_anchor": section, "quote": s})      # noqa: E731
        if section and section.startswith("§1"):
            d = re.findall(r"\d{4}-\d{2}-\d{2}", s)
            if len(d) >= 2:
                add("term", "start_date", d[0])
                add("term", "end_date", d[1])
            n = re.search(r"(\d+) days (?:written )?notice", s)
            if n:
                add("renewal", "notice_days", int(n.group(1)))
        if section and section.startswith("§2"):
            n = re.search(r"(?:net|within)\s+(\d+)", s, re.I)
            if n:
                add("payment", "net_days", int(n.group(1)))
        if section and section.startswith("§3"):
            n = re.match(r"([A-Za-z][A-Za-z ]+?)\s*(?::|-|,)\s*(?:\$|USD\s)?([\d,]+\.\d{2})\s*(?:per hour|/hour|dollars per hour)", s)
            if n:
                add("rate_card", n.group(1).strip(), float(n.group(2).replace(",", "")))
        if section and section.startswith("§4"):
            thr, pct = re.search(r"\$([\d,]+)", s), re.search(r"(\d+(?:\.\d+)?)%", s)
            if thr and pct:
                add("rebate", "threshold", float(thr.group(1).replace(",", "")))
                add("rebate", "percent", float(pct.group(1)))
        if section and section.startswith("§5"):
            pct = re.search(r"(\d+(?:\.\d+)?)%", s)
            if pct:
                add("price_cap", "max_increase_percent", float(pct.group(1)))
    return terms


# ---------------------------------------------------------------- leakage-engine

LINE = re.compile(r"(?:Contractor\s+)?(?P<role>[A-Za-z][A-Za-z ]+?)\s+(?:-|week ending)\s*(?P<hrs>\d+(?:\.\d+)?)\s*hrs\s*@\s*(?P<rate>[\d.]+)")


def rate_leakage(lines, rate_card):
    """Invoice lines billed above the contracted hourly rate. lines: [{id, description, qty, unit_price}]; rate_card: {role: (rate, term id)}."""
    out = []
    for ln in lines:
        m = LINE.search(ln["description"])
        if not m:
            continue
        role = m.group("role").strip()
        if role in rate_card:
            agreed, term = rate_card[role]
            if float(ln["unit_price"]) > agreed + 0.005:
                qty = float(ln["qty"])
                out.append({"invoice_line_id": ln["id"], "contract_term_id": term, "expected_amount": round(agreed * qty, 2), "actual_amount": round(float(ln["unit_price"]) * qty, 2),
                            "variance": round((float(ln["unit_price"]) - agreed) * qty, 2), "confidence": 0.98, "kind": "rate_above_contract",
                            "detail": f"{role}: billed ${float(ln['unit_price']):.2f}/h, contract ${agreed:.2f}/h, {qty:g} h"})
    return out


def rebate_leakage(spend, credited, threshold, percent):
    """A volume rebate that was earned and not (fully) credited."""
    if spend <= threshold:
        return None
    owed = round(spend * percent / 100, 2)
    missing = round(owed - credited, 2)
    return {"expected_amount": owed, "actual_amount": credited, "variance": missing, "confidence": 0.9} if missing > 1 else None


# ---------------------------------------------------------------- opportunity-engine

def score(savings, confidence, effort, months_to_renewal=None):
    """Rank = savings x confidence x timing / effort. Money that is simply owed back is easy; switching suppliers is not,
    and is worth more when a renewal is near."""
    timing = 1.0 if months_to_renewal is None else 1.3 if months_to_renewal <= 6 else 0.8
    return round(savings * confidence * timing / {"low": 1.0, "medium": 2.0, "high": 4.0}[effort], 2)
