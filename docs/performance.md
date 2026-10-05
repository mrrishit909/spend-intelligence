# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 41,021 invoices, 116,197 invoice lines,
985 vendor records resolved to 413 companies. Reproduce with `make demo && make load-test`.

## Reads: leakage summary, spend cube (category by quarter), duplicate invoices, vendor search (in rotation), closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 893 | 89.0 req/s | 5.9 ms | 29.1 ms | 32.9 ms | 0.00% |
| 8 | 3361 | 335.7 req/s | 18.0 ms | 46.9 ms | 55.8 ms | 0.00% |
| 32 | 2570 | 254.7 req/s | 95.6 ms | 173.2 ms | 261.8 ms | 0.00% |

Target: p95 under 750 ms for interactive reads: met, after a change.

**What the first run found.** 10 requests a second with a p95 of 1.7 s at 8 clients. Three of the four endpoints rescanned the whole
ledger on every request (grouping 41,000 invoices for duplicates, aggregating 116,000 lines for the cube). Those scans are now
memoised per tenant and recomputed only when the data version moves (invoice count, vendor count, latest human decision,
uncategorized count). The memo lives in process memory: with several API processes each would compute once.

## Compute

| Work | Time |
|---|---|
| Seed: fit and score the two models on separate synthetic worlds | about 9 s |
| Import job: one year, 41,021 invoices + 116,197 lines + 24,428 purchase orders | about 6 s |
| Vendor resolution: 985 records, blocking, pair scoring, clustering, re-pointing invoices | about 2 s |
| Classify 29,150 uncategorized lines | about 2 s |
| Leakage scan: read 129 contracts, check every invoice line of contracted vendors | about 2 s |

Bulk loads into tenant tables go through a temporary table, because `COPY` cannot target a table with row-level security.

Not measured: the blueprint's "millions of invoice lines" (three years here is 348,000 lines; `fiscal_year` imports one at a time).
