-- ============================================================
-- Qmemory Cloud — Multi-User Additions
-- SurrealDB 3.0+
--
-- ADDITIVE: Does not replace schema.surql — adds user auth,
-- API tokens, and owner fields for multi-tenancy.
--
-- IDEMPOTENT: Safe to re-run (IF NOT EXISTS on everything)
-- ============================================================

-- ============================================================
-- NODE: user (multi-user accounts)
-- ============================================================

DEFINE TABLE IF NOT EXISTS user SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS email      ON user TYPE string;
DEFINE FIELD IF NOT EXISTS password   ON user TYPE string;
DEFINE FIELD IF NOT EXISTS name       ON user TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON user TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS email_unique ON user FIELDS email UNIQUE;

-- ============================================================
-- ACCESS: qmemory_user (signup/signin with Argon2 hashing)
-- ============================================================

DEFINE ACCESS OVERWRITE qmemory_user ON DATABASE TYPE RECORD
  SIGNUP (
    CREATE user CONTENT {
      email: $email,
      password: crypto::argon2::generate($password),
      name: $name,
      created_at: time::now()
    }
  )
  SIGNIN (
    SELECT * FROM user
    WHERE email = $email
    AND crypto::argon2::compare(password, $password)
  )
  DURATION FOR TOKEN 24h, FOR SESSION 7d;

-- ============================================================
-- NODE: api_token (API key management)
-- ============================================================

DEFINE TABLE IF NOT EXISTS api_token SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS user       ON api_token TYPE record<user>;
DEFINE FIELD IF NOT EXISTS token_hash ON api_token TYPE string;
DEFINE FIELD IF NOT EXISTS prefix     ON api_token TYPE string;
DEFINE FIELD IF NOT EXISTS name       ON api_token TYPE string DEFAULT 'Default';
DEFINE FIELD IF NOT EXISTS created_at ON api_token TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at ON api_token TYPE datetime;
DEFINE FIELD IF NOT EXISTS last_used  ON api_token TYPE option<datetime>;
DEFINE INDEX IF NOT EXISTS token_hash_idx ON api_token FIELDS token_hash UNIQUE;

-- ============================================================
-- Owner fields on existing tables (option so existing records
-- with no owner still work — backwards compatible)
-- ============================================================

DEFINE FIELD IF NOT EXISTS owner ON memory TYPE option<record<user>>;
DEFINE FIELD IF NOT EXISTS owner ON entity TYPE option<record<user>>;

-- ============================================================
-- Linker tracking + recall fields on memory
-- (these may already exist in base schema — IF NOT EXISTS
-- makes this safe to re-run either way)
-- ============================================================

DEFINE FIELD IF NOT EXISTS linked        ON memory TYPE bool DEFAULT false;
DEFINE INDEX IF NOT EXISTS linked_idx    ON memory FIELDS linked;
DEFINE FIELD IF NOT EXISTS recall_count  ON memory TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS last_recalled ON memory TYPE option<datetime>;
