-- A native book as the last schema-13 alpha left it: one account, no transactions.
BEGIN TRANSACTION;
CREATE TABLE account (
    handle TEXT PRIMARY KEY,
    parent TEXT,
    name   TEXT NOT NULL,
    atype  TEXT NOT NULL,
    blob   TEXT NOT NULL
);
INSERT INTO "account" VALUES('fixture-account',NULL,'Fixture Checking','BANK','{"_schema":1,"handle":"fixture-account","gid":"","change":1794279526,"private":false,"tags":[],"name":"Fixture Checking","atype":"BANK","parent":null,"commodity":null,"code":"","description":"","placeholder":false,"hidden":false,"commodity_scu":null,"notes":"","imported_notes_digest":"","source_atype":null,"source_guid":null,"source_type":"","source_fields":[],"fsa_years":[],"fsa_dependent_care":false,"annual_return":"0","cost_basis_method":"fifo","annual_interest":"0","exclude_from_projection":false,"group":"","linked_asset":null,"pays_in_full":true,"usual_payment":null,"payment_day":null,"card_payment_account":null,"emergency_fund":null}');
CREATE TABLE commodity (
    handle   TEXT PRIMARY KEY,
    mnemonic TEXT NOT NULL,
    blob     TEXT NOT NULL
);
INSERT INTO "commodity" VALUES('00000000000000000000000000000840','USD','{"_schema":1,"handle":"00000000000000000000000000000840","gid":"","change":0,"private":false,"tags":[],"namespace":"CURRENCY","mnemonic":"USD","fullname":"US Dollar","fraction":100,"symbol":"$"}');
CREATE TABLE fsa_claim (
    handle       TEXT PRIMARY KEY,
    service_date TEXT NOT NULL,
    provider     TEXT NOT NULL,
    blob         TEXT NOT NULL
);
CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO "metadata" VALUES('default_currency','"00000000000000000000000000000840"');
INSERT INTO "metadata" VALUES('schema_version','13');
INSERT INTO "metadata" VALUES('fixture_marker','"schema-13"');
INSERT INTO "metadata" VALUES('book_revision','1');
CREATE TABLE price (
    handle     TEXT PRIMARY KEY,
    commodity  TEXT NOT NULL,
    currency   TEXT NOT NULL,
    quote_date TEXT NOT NULL,
    source     TEXT NOT NULL,
    blob       TEXT NOT NULL
);
CREATE TABLE receivable (
    handle        TEXT PRIMARY KEY,
    incurred_date TEXT NOT NULL,
    payer         TEXT NOT NULL,
    blob          TEXT NOT NULL
);
CREATE TABLE reconciliation (
    handle         TEXT PRIMARY KEY,
    account        TEXT NOT NULL,
    statement_date TEXT NOT NULL,
    status         TEXT NOT NULL,
    blob           TEXT NOT NULL
);
CREATE TABLE savings_goal (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE scenario (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE scheduled (
    handle TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    blob   TEXT NOT NULL
);
CREATE TABLE schema_migration (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO "schema_migration" VALUES(13,'fixture');
CREATE TABLE split_index (
    handle    TEXT PRIMARY KEY,
    txn       TEXT NOT NULL,
    account   TEXT NOT NULL,
    post_date TEXT NOT NULL,
    value_num INTEGER NOT NULL,
    value_den INTEGER NOT NULL,
    quantity_num INTEGER NOT NULL DEFAULT 0,
    quantity_den INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE txn (
    handle      TEXT PRIMARY KEY,
    post_date   TEXT NOT NULL,
    description TEXT,
    blob        TEXT NOT NULL
);
CREATE INDEX idx_commodity_mnemonic ON commodity(mnemonic);
CREATE INDEX idx_price_lookup
    ON price(commodity, currency, quote_date);
CREATE INDEX idx_account_parent ON account(parent);
CREATE INDEX idx_txn_date ON txn(post_date);
CREATE INDEX idx_split_account ON split_index(account, post_date);
CREATE INDEX idx_split_txn ON split_index(txn);
CREATE INDEX idx_fsa_claim_service_date ON fsa_claim(service_date);
CREATE INDEX idx_reconciliation_account_date
    ON reconciliation(account, statement_date);
CREATE INDEX idx_receivable_incurred_date ON receivable(incurred_date);
COMMIT;
