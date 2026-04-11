-- Qmemory admin database schema.
-- Only one table: user. Lives in qmemory.admin database.

DEFINE TABLE IF NOT EXISTS user SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS user_code ON user TYPE string
    ASSERT string::len($value) BETWEEN 4 AND 64
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS display_name ON user TYPE string
    ASSERT string::len($value) BETWEEN 1 AND 128
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS db_name ON user TYPE string
    ASSERT string::len($value) BETWEEN 4 AND 128
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS created_at ON user TYPE datetime
    VALUE $value OR time::now()
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS last_active_at ON user TYPE option<datetime>
    PERMISSIONS NONE;

DEFINE FIELD IF NOT EXISTS is_active ON user TYPE bool
    DEFAULT true
    PERMISSIONS NONE;

DEFINE INDEX IF NOT EXISTS idx_user_code ON user FIELDS user_code UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_user_is_active ON user FIELDS is_active;
