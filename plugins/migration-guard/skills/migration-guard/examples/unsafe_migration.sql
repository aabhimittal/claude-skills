-- Example: a migration full of operations that lock or rewrite a large table.
-- Run: analyze_migration.py unsafe_migration.sql

BEGIN;

-- Rewrites every row to populate the volatile default (ACCESS EXCLUSIVE lock).
ALTER TABLE users ADD COLUMN signup_token uuid NOT NULL DEFAULT gen_random_uuid();

-- Blocks all writes to a large table for the whole build.
CREATE INDEX idx_users_email ON users (email);

-- Cannot run inside this BEGIN/COMMIT block — will error at runtime.
CREATE INDEX CONCURRENTLY idx_users_status ON users (status);

-- Validates every existing row while locking both tables.
ALTER TABLE orders ADD CONSTRAINT orders_user_fk
    FOREIGN KEY (user_id) REFERENCES users (id);

-- Full table rewrite under an exclusive lock.
ALTER TABLE users ALTER COLUMN external_id TYPE bigint;

-- Unbounded write: locks every row, bloats WAL, lags replicas.
UPDATE users SET active = true;

COMMIT;
