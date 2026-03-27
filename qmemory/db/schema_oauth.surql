-- ============================================================
-- Qmemory Cloud — OAuth 2.0 Schema
-- SurrealDB 3.0+
--
-- OAuth 2.0 support for Claude.ai and other OAuth clients.
-- MUST run AFTER schema.surql and schema_cloud.surql
--
-- Flow:
-- 1. Claude.ai redirects user to /oauth/authorize
-- 2. User logs in and consents
-- 3. We generate authorization code and redirect back
-- 4. Claude.ai exchanges code for access token at /oauth/token
-- 5. Access token is the same qm_ak_xxx format used for manual tokens
-- ============================================================

-- ============================================================
-- OAuth Client Table
-- Stores registered OAuth applications (e.g., Claude.ai)
-- ============================================================
DEFINE TABLE IF NOT EXISTS oauth_client TYPE NORMAL SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS id ON oauth_client TYPE string;
DEFINE FIELD IF NOT EXISTS name ON oauth_client TYPE string;
DEFINE FIELD IF NOT EXISTS secret_hash ON oauth_client TYPE option<string>;  -- SHA-256 of client secret (optional for public/PKCE clients)
DEFINE FIELD IF NOT EXISTS redirect_uris ON oauth_client TYPE array;
DEFINE FIELD IF NOT EXISTS allowed_scopes ON oauth_client TYPE array DEFAULT ["read", "write"];
DEFINE FIELD IF NOT EXISTS grant_types ON oauth_client TYPE array DEFAULT ["authorization_code"];
DEFINE FIELD IF NOT EXISTS token_endpoint_auth_method ON oauth_client TYPE string DEFAULT "none";  -- "none" for PKCE public clients
DEFINE FIELD IF NOT EXISTS created_at ON oauth_client TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_oauth_client_id ON oauth_client FIELDS id UNIQUE;

-- ============================================================
-- OAuth Authorization Code Table
-- Short-lived codes exchanged for access tokens
-- ============================================================
DEFINE TABLE IF NOT EXISTS oauth_authorization_code TYPE NORMAL SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS code ON oauth_authorization_code TYPE string;  -- Random 32-char code
DEFINE FIELD IF NOT EXISTS client_id ON oauth_authorization_code TYPE record<oauth_client>;
DEFINE FIELD IF NOT EXISTS user_id ON oauth_authorization_code TYPE record<user>;
DEFINE FIELD IF NOT EXISTS redirect_uri ON oauth_authorization_code TYPE string;
DEFINE FIELD IF NOT EXISTS scope ON oauth_authorization_code TYPE string;
DEFINE FIELD IF NOT EXISTS state ON oauth_authorization_code TYPE option<string>;  -- CSRF protection
DEFINE FIELD IF NOT EXISTS code_challenge ON oauth_authorization_code TYPE option<string>;  -- PKCE
DEFINE FIELD IF NOT EXISTS code_challenge_method ON oauth_authorization_code TYPE option<string>;  -- "S256" or "plain"
DEFINE FIELD IF NOT EXISTS expires_at ON oauth_authorization_code TYPE datetime;
DEFINE FIELD IF NOT EXISTS used ON oauth_authorization_code TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON oauth_authorization_code TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_oauth_code ON oauth_authorization_code FIELDS code UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_oauth_code_expires ON oauth_authorization_code FIELDS expires_at;
