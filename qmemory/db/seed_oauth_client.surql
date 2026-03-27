-- ============================================================
-- Seed OAuth Client: Claude.ai
-- ============================================================
-- Run this to register Claude.ai as an OAuth client.
--
-- Usage:
--   surreal import -e <url> -u root -p <pass> --namespace qmemory --database main seed_oauth_client.surql
--
-- Client ID: claude-ai
-- Client Secret: Mx6fGNy_kbJ9EUpxQQtlN8W8JeYmQ7T_6cawlwx6qzU
-- (Store the secret securely in Railway env vars)
-- ============================================================

INSERT IGNORE INTO oauth_client {
    id: "claude-ai",
    name: "Claude.ai",
    secret_hash: "92742342f2d40e5f4c5b688d289408b0541804131ca16d76227b606fc2dd8786",
    redirect_uris: [
        "https://claude.ai/oauth/callback",
        "https://claude.ai/api/mcp/callback",
        "https://claude.ai/api/mcp/auth_callback"
    ],
    allowed_scopes: ["read", "write"],
    created_at: time::now()
};
