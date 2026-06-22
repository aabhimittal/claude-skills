-- Example: the same intent as unsafe_migration.sql, rewritten to be online-safe.
-- analyze_migration.py reports no high/critical findings for this file.
--
-- Concurrent index builds must NOT be wrapped in a transaction, so this file
-- deliberately avoids BEGIN/COMMIT. Each statement is independently safe.

-- Fail fast instead of stalling the table behind a contended lock.
SET lock_timeout = '5s';

-- 1. Add the column nullable with no volatile default (metadata-only).
ALTER TABLE users ADD COLUMN signup_token uuid;

-- 2. Backfill in bounded batches from application code or a data migration,
--    e.g. UPDATE users SET signup_token = gen_random_uuid()
--         WHERE id BETWEEN $1 AND $2;  (repeat over id ranges)

-- 3. Build indexes without blocking writes (outside any transaction).
CREATE INDEX CONCURRENTLY idx_users_email ON users (email);
CREATE INDEX CONCURRENTLY idx_users_status ON users (status);

-- 4. Add the foreign key without scanning existing rows, then validate it
--    under a lighter SHARE UPDATE EXCLUSIVE lock.
ALTER TABLE orders ADD CONSTRAINT orders_user_fk
    FOREIGN KEY (user_id) REFERENCES users (id) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_user_fk;
