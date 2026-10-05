-- Domain model. Names follow the blueprint's persistence model; additions are marked (+).

CREATE TABLE vendors (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, canonical_name text NOT NULL, tax_id_hash text, country text, risk_json jsonb NOT NULL DEFAULT '{}');
SELECT enable_tenant_rls('vendors');

CREATE TABLE vendor_aliases (id uuid PRIMARY KEY, vendor_id uuid REFERENCES vendors ON DELETE SET NULL, source_system text NOT NULL, source_vendor_id text NOT NULL, raw_name text NOT NULL, match_confidence numeric,
  tenant_id uuid NOT NULL REFERENCES tenants, tax_id_hash text, bank_hash text, address text,                              -- (+) what the source systems sent
  UNIQUE (tenant_id, source_system, source_vendor_id));
SELECT enable_tenant_rls('vendor_aliases');

CREATE TABLE vendor_decisions (tenant_id uuid NOT NULL REFERENCES tenants, alias_a uuid NOT NULL REFERENCES vendor_aliases, alias_b uuid NOT NULL REFERENCES vendor_aliases, same boolean NOT NULL,
  decided_by uuid NOT NULL, decided_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (alias_a, alias_b));               -- (+) human merge / split feedback
SELECT enable_tenant_rls('vendor_decisions');

CREATE TABLE spend_categories (id uuid PRIMARY KEY, parent_id uuid REFERENCES spend_categories, code text NOT NULL UNIQUE, name text NOT NULL, level int NOT NULL);

CREATE TABLE purchase_orders (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, vendor_id uuid REFERENCES vendors ON DELETE SET NULL, po_number text NOT NULL, ordered_at date NOT NULL, total numeric,
  currency text NOT NULL DEFAULT 'USD', category_id uuid REFERENCES spend_categories);
SELECT enable_tenant_rls('purchase_orders');

CREATE TABLE invoices (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, vendor_id uuid REFERENCES vendors ON DELETE SET NULL, invoice_number text NOT NULL, invoice_date date NOT NULL, due_date date NOT NULL,
  total numeric NOT NULL, currency text NOT NULL DEFAULT 'USD', po_id uuid REFERENCES purchase_orders, fingerprint text NOT NULL,
  alias_id uuid NOT NULL REFERENCES vendor_aliases, business_unit text NOT NULL);                                           -- (+)
CREATE INDEX invoices_vendor ON invoices (tenant_id, vendor_id, invoice_date);
CREATE INDEX invoices_alias ON invoices (alias_id);
SELECT enable_tenant_rls('invoices');

CREATE TABLE invoice_lines (id uuid PRIMARY KEY, invoice_id uuid NOT NULL REFERENCES invoices ON DELETE CASCADE, description text NOT NULL, qty numeric NOT NULL, unit_price numeric NOT NULL, amount numeric NOT NULL,
  category_id uuid REFERENCES spend_categories,
  category_source text, category_confidence numeric);                                                                       -- (+) 'source' | 'model'
CREATE INDEX invoice_lines_invoice ON invoice_lines (invoice_id);
CREATE INDEX invoice_lines_uncategorized ON invoice_lines (invoice_id) WHERE category_id IS NULL;

CREATE TABLE contracts (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, vendor_id uuid REFERENCES vendors ON DELETE SET NULL, title text NOT NULL, start_date date, end_date date, object_uri text NOT NULL,
  status text NOT NULL, alias_id uuid NOT NULL REFERENCES vendor_aliases, body text NOT NULL);                              -- (+) the document text stands in for object storage
SELECT enable_tenant_rls('contracts');
CREATE TABLE contract_terms (id uuid PRIMARY KEY, contract_id uuid NOT NULL REFERENCES contracts ON DELETE CASCADE, term_type text NOT NULL, key text NOT NULL, value_json jsonb NOT NULL, citation_anchor text NOT NULL,
  quote text NOT NULL);                                                                                                      -- (+) the sentence the term was read from

CREATE TABLE leakage_findings (id uuid PRIMARY KEY, invoice_line_id uuid REFERENCES invoice_lines ON DELETE CASCADE, contract_term_id uuid REFERENCES contract_terms ON DELETE CASCADE, expected_amount numeric NOT NULL,
  actual_amount numeric NOT NULL, variance numeric NOT NULL, confidence numeric NOT NULL, status text NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenants, vendor_id uuid REFERENCES vendors ON DELETE CASCADE, kind text NOT NULL, detail text NOT NULL);     -- (+)
SELECT enable_tenant_rls('leakage_findings');

CREATE TABLE opportunities (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, opportunity_type text NOT NULL, vendor_ids uuid[] NOT NULL, estimated_savings numeric NOT NULL, effort text NOT NULL,
  evidence jsonb NOT NULL, status text NOT NULL, score numeric NOT NULL, title text NOT NULL);                              -- (+)
SELECT enable_tenant_rls('opportunities');
