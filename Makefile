export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55107/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8107

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m sp.seed --if-empty
dev:            ## API with reload on :8107, jobs run by a local worker
	($(PY) -m core.jobs sp.api &) && $(PY) -m uvicorn sp.api:app --reload --port 8107
test:
	$(PY) -m pytest --cov=sp --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the six-step scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first
	$(PY) -m core.loadtest $(BASE) acme-analyst-demo 32 10 "GET /v1/leakage" "GET /v1/spend/cube?rows=category&cols=quarter" "GET /v1/invoices/duplicates" "GET /v1/vendors?q=cloud"
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
