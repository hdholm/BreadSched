CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO metadata VALUES ('schema_version', '6');
INSERT INTO metadata VALUES ('fixture_marker', '"schema-6"');

CREATE TABLE commodity (handle TEXT PRIMARY KEY, mnemonic TEXT NOT NULL, blob TEXT NOT NULL);
CREATE INDEX idx_commodity_mnemonic ON commodity(mnemonic);
CREATE TABLE price (handle TEXT PRIMARY KEY, commodity TEXT NOT NULL, currency TEXT NOT NULL, quote_date TEXT NOT NULL, source TEXT NOT NULL, blob TEXT NOT NULL);
CREATE INDEX idx_price_lookup ON price(commodity, currency, quote_date);
CREATE TABLE account (handle TEXT PRIMARY KEY, parent TEXT, name TEXT NOT NULL, atype TEXT NOT NULL, blob TEXT NOT NULL);
CREATE INDEX idx_account_parent ON account(parent);
INSERT INTO account VALUES (
    'fixture-account', NULL, 'Fixture Checking', 'BANK',
    '{"_schema":1,"handle":"fixture-account","gid":"","change":0,"private":false,"tags":[],"name":"Fixture Checking","atype":"BANK","parent":null,"commodity":null,"code":"","description":"","placeholder":false,"hidden":false,"commodity_scu":null,"notes":"","source_notes":"","source_atype":null,"source_guid":null,"source_type":"","source_fields":[],"fsa_years":[],"annual_return":"0","annual_interest":"0","exclude_from_projection":false,"group":"","linked_asset":null,"pays_in_full":true,"usual_payment":null,"payment_day":null,"card_payment_account":null,"emergency_fund":null}'
);
CREATE TABLE txn (handle TEXT PRIMARY KEY, post_date TEXT NOT NULL, description TEXT, blob TEXT NOT NULL);
CREATE INDEX idx_txn_date ON txn(post_date);
CREATE TABLE split_index (handle TEXT PRIMARY KEY, txn TEXT NOT NULL, account TEXT NOT NULL, post_date TEXT NOT NULL, value_num INTEGER NOT NULL, value_den INTEGER NOT NULL, quantity_num INTEGER NOT NULL DEFAULT 0, quantity_den INTEGER NOT NULL DEFAULT 1);
CREATE INDEX idx_split_account ON split_index(account, post_date);
CREATE INDEX idx_split_txn ON split_index(txn);
CREATE TABLE scheduled (handle TEXT PRIMARY KEY, name TEXT NOT NULL, blob TEXT NOT NULL);
CREATE TABLE scenario (handle TEXT PRIMARY KEY, name TEXT NOT NULL, blob TEXT NOT NULL);
CREATE TABLE fsa_claim (handle TEXT PRIMARY KEY, service_date TEXT NOT NULL, provider TEXT NOT NULL, blob TEXT NOT NULL);
CREATE INDEX idx_fsa_claim_service_date ON fsa_claim(service_date);
CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT INTO schema_migration(version) VALUES (6);
