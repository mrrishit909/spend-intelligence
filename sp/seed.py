"""Tenants, the category taxonomy, and the two models (trained on a separate synthetic world, then registered).
The AP data itself is loaded by the import job, as it would be from an ERP extract.

    python -m sp.seed [--if-empty]
"""
import hashlib
import pickle
import sys
import uuid

from psycopg.types.json import Jsonb

from core import db

from . import engine, world

TENANTS = {"acme": ("Acme Holdings", 1.0, 29), "other": ("Second Tenant Ltd", 0.1, 41)}
ROLES = ["analyst", "procurement", "admin"]
NS = uuid.UUID("7a1d3c0a-0000-4000-8000-00000000c0de")


def token(tenant, role):
    """Demo credentials for local use only."""
    return f"{tenant}-{role}-demo"


def cat_id(name):
    return uuid.uuid5(NS, name)


def train():
    """Fit on a world built from a different seed than any tenant's, score on a second one: the models never see the data they are used on."""
    a, b = world.build(0.5, seed=101), world.build(0.5, seed=202)
    truth = lambda w: [x["vendor"] for x in w["aliases"]]      # noqa: E731
    resolver = engine.train_resolver(a["aliases"], truth(a))
    er = engine.pair_scores(engine.resolve(b["aliases"], resolver)[0], truth(b))
    er_base = engine.pair_scores(engine.resolve(b["aliases"])[0], truth(b))
    clf = engine.train_classifier([x["description"] for x in a["lines"][:80000]], [x["category"] for x in a["lines"][:80000]])
    te = b["lines"][:40000]
    pred = clf.predict([x["description"] for x in te])
    acc = sum(p == x["category"] for p, x in zip(pred, te)) / len(te)
    base = sum(engine.keyword_category(x["description"]) == x["category"] for x in te) / len(te)
    return {"resolver": resolver, "classifier": clf}, {"entity_resolution": {"model": er, "baseline": {"name": "identical normalized name", **er_base}},
                                                       "classification": {"model": {"accuracy": round(acc, 4), "n_test": len(te)}, "baseline": {"name": "keyword rules", "accuracy": round(base, 4)}}}


def main(if_empty=False):
    db.migrate()
    with db.tx() as c:
        if if_empty and c.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            return print("already seeded")
    bundle, metrics = train()
    out = {"metrics": metrics}
    with db.tx() as c:
        c.execute("INSERT INTO model_registry (name, version, data_snapshot, metrics, baseline, artifact) VALUES ('spend-models',%s,%s,%s,%s,%s)",
                  [engine.VERSION, "synthetic worlds, seeds 101 (fit) and 202 (score)", Jsonb({k: v["model"] for k, v in metrics.items()}), Jsonb({k: v["baseline"] for k, v in metrics.items()}), pickle.dumps(bundle)])
        parents = sorted({v[0] for v in world.CATEGORIES.values()})
        for i, p in enumerate(parents):
            c.execute("INSERT INTO spend_categories VALUES (%s,NULL,%s,%s,1)", [cat_id(p), f"L1-{i + 1:02d}", p])
        for i, (name, v) in enumerate(world.CATEGORIES.items()):
            c.execute("INSERT INTO spend_categories VALUES (%s,%s,%s,%s,2)", [cat_id(name), cat_id(v[0]), f"L2-{i + 1:02d}", name])
        for key, (name, _, _) in TENANTS.items():
            t = uuid.uuid5(uuid.NAMESPACE_DNS, f"{key}.spend-intelligence.example")
            c.execute("INSERT INTO tenants (id, name) VALUES (%s,%s)", [t, name])
            for role in ROLES:
                c.execute("INSERT INTO api_tokens VALUES (%s,%s,%s,%s,%s)", [hashlib.sha256(token(key, role).encode()).hexdigest(), t, uuid.uuid5(t, role), f"{role} ({name})", role])
            out[key] = t
    print("models registered:", metrics["entity_resolution"]["model"], metrics["classification"]["model"])
    print("demo tokens:", ", ".join(token(t, r) for t in TENANTS for r in ROLES))
    return out


if __name__ == "__main__":
    main("--if-empty" in sys.argv)
