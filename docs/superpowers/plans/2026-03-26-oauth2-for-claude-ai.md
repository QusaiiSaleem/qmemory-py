# OAuth 2.0 for Claude.ai Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OAuth 2.0 support to qmemory so Claude.ai web app can connect to the MCP server, while keeping the existing Bearer token auth for Claude Code CLI.

**Architecture:**
- **Bearer Token Auth** (existing): Used by Claude Code CLI, direct API calls - simple `Authorization: Bearer qm_ak_xxx` header
- **OAuth 2.0** (new): Used by Claude.ai web app - standard authorization code flow with client credentials

**Tech Stack:** FastAPI, SurrealDB, OAuth 2.0 (Authorization Code Flow)

---

## OAuth 2.0 Flow Overview

```
┌──────────┐     ┌──────────┐     ┌───────────┐     ┌──────────┐
│ Claude.ai│────>│ /oauth/  │────>│  User     │────>│ /oauth/  │
│          │     │ authorize│     │  Login &  │     │  consent │
│          │     │          │     │  Consent  │     │          │
└──────────┘     └──────────┘     └───────────┘     └──────────┘
                                                            │
      ┌─────────────────────────────────────────────────────┘
      │
      ▼
┌──────────┐     ┌──────────┐     ┌───────────┐
│ Claude.ai│────>│ /oauth/  │────>│ Access    │
│          │     │  token   │     │ Token     │
│          │     │          │     │ (existing │
│          │     │          │     │  qm_ak_)  │
└──────────┘     └──────────┘     └───────────┘
```

**Key Insight:** The OAuth flow generates the same `qm_ak_xxx` tokens that users currently create manually. Claude.ai just automates this via OAuth.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `qmemory/app/routes/oauth.py` | Create | OAuth 2.0 endpoints (authorize, token, consent) |
| `qmemory/db/schema_oauth.surql` | Create | OAuth client table, authorization code table |
| `qmemory/app/main.py` | Modify | Mount OAuth routes |
| `qmemory/app/templates/pages/consent.html` | Create | User consent page |
| `qmemory/app/auth.py` | Modify | Support OAuth token validation |
| `tests/test_oauth.py` | Create | OAuth flow tests |

---

## Database Schema

### oauth_client table
Stores registered OAuth clients (only Claude.ai in our case):

```sql
DEFINE TABLE oauth_client TYPE NORMAL SCHEMAFULL
  PERMISSIONS NONE;

DEFINE FIELD id ON oauth_client TYPE string;  -- e.g., "claude-ai"
DEFINE FIELD name ON oauth_client TYPE string;
DEFINE FIELD secret_hash ON oauth_client TYPE string;  -- SHA-256 of client secret
DEFINE FIELD redirect_uris ON oauth_client TYPE array<string>;
DEFINE FIELD allowed_scopes ON oauth_client TYPE array<string> DEFAULT ["read", "write"];
DEFINE FIELD created_at ON oauth_client TYPE datetime DEFAULT time::now();
DEFINE INDEX idx_oauth_client_id ON oauth_client FIELDS id UNIQUE;
```

### oauth_authorization_code table
Temporary storage for authorization codes (short-lived):

```sql
DEFINE TABLE oauth_authorization_code TYPE NORMAL SCHEMAFULL
  PERMISSIONS NONE;

DEFINE FIELD code ON oauth_authorization_code TYPE string;  -- Random code
DEFINE FIELD client_id ON oauth_authorization_code TYPE record<oauth_client>;
DEFINE FIELD user_id ON oauth_authorization_code TYPE record<user>;
DEFINE FIELD redirect_uri ON oauth_authorization_code TYPE string;
DEFINE FIELD scope ON oauth_authorization_code TYPE string;
DEFINE FIELD expires_at ON oauth_authorization_code TYPE datetime;
DEFINE FIELD used ON oauth_authorization_code TYPE bool DEFAULT false;
DEFINE INDEX idx_oauth_code ON oauth_authorization_code FIELDS code UNIQUE;
DEFINE INDEX idx_oauth_code_expires ON oauth_authorization_code FIELDS expires_at;
```

---

## API Endpoints

### GET /oauth/authorize
**Purpose:** Start OAuth flow - redirect to login or show consent

**Query Params:**
- `response_type` - Must be "code"
- `client_id` - OAuth client ID (e.g., "claude-ai")
- `redirect_uri` - Where to redirect after consent
- `scope` - Requested scopes (e.g., "read write")
- `state` - CSRF protection token (required)

**Flow:**
1. Validate client_id and redirect_uri
2. If user not logged in → redirect to /login with return URL
3. If user logged in → show consent page
4. On consent → generate authorization code → redirect to redirect_uri with code

### POST /oauth/consent
**Purpose:** Handle user's consent decision

**Form Data:**
- `code` - Authorization code (if approved)
- `denied` - If user denied access

### POST /oauth/token
**Purpose:** Exchange authorization code for access token

**Form Data:**
- `grant_type` - Must be "authorization_code"
- `code` - Authorization code from /oauth/authorize
- `redirect_uri` - Must match the one from authorize request
- `client_id` - OAuth client ID
- `client_secret` - OAuth client secret

**Response:**
```json
{
  "access_token": "qm_ak_xxxxx",
  "token_type": "Bearer",
  "expires_in": 2592000,
  "scope": "read write"
}
```

---

## Task List

### Task 1: Create OAuth database schema
**Files:**
- Create: `qmemory/db/schema_oauth.surql`

- [ ] **Step 1: Create schema file with oauth_client and oauth_authorization_code tables**
- [ ] **Step 2: Add cleanup job for expired authorization codes**
- [ ] **Step 3: Test schema applies correctly**

### Task 2: Create OAuth routes module
**Files:**
- Create: `qmemory/app/routes/oauth.py`

- [ ] **Step 1: Create the authorize endpoint (GET /oauth/authorize)**
- [ ] **Step 2: Create the consent endpoint (POST /oauth/consent)**
- [ ] **Step 3: Create the token endpoint (POST /oauth/token)**
- [ ] **Step 4: Add OAuth client validation helpers**

### Task 3: Create consent page template
**Files:**
- Create: `qmemory/app/templates/pages/consent.html`

- [ ] **Step 1: Create consent page with app name, scopes, allow/deny buttons**
- [ ] **Step 2: Style to match existing qmemory UI**

### Task 4: Register Claude.ai as OAuth client
**Files:**
- None (database operation)

- [ ] **Step 1: Generate client credentials for Claude.ai**
- [ ] **Step 2: Insert into oauth_client table**
- [ ] **Step 3: Document the client_id for users**

### Task 5: Update main.py to mount OAuth routes
**Files:**
- Modify: `qmemory/app/main.py`

- [ ] **Step 1: Import and mount OAuth router**
- [ ] **Step 2: Add OAuth routes to startup log**

### Task 6: Create tests
**Files:**
- Create: `tests/test_oauth.py`

- [ ] **Step 1: Test full authorization code flow**
- [ ] **Step 2: Test invalid client rejection**
- [ ] **Step 3: Test expired code rejection**
- [ ] **Step 4: Test state parameter CSRF protection**

### Task 7: Update /connect page with Claude.ai instructions
**Files:**
- Modify: `qmemory/app/templates/pages/connect.html`

- [ ] **Step 1: Update Claude.ai tab to show OAuth flow**
- [ ] **Step 2: Add client_id that users should use**

### Task 8: Deploy and test end-to-end
**Files:**
- None (manual testing)

- [ ] **Step 1: Deploy to Railway**
- [ ] **Step 2: Test with Claude.ai MCP connector**
- [ ] **Step 3: Verify existing Bearer token auth still works**

---

## What Stays Unchanged

- **Bearer Token Auth** - The existing `Authorization: Bearer qm_ak_xxx` header continues to work for:
  - Claude Code CLI
  - Direct API calls
  - Any HTTP client
- **Token Generation** - Users can still manually generate tokens at `/tokens`
- **MCP Endpoints** - All MCP tools work identically regardless of auth method
- **Database Isolation** - User database routing works the same way

---

## Security Considerations

1. **PKCE Support** - Consider adding PKCE (Proof Key for Code Exchange) for enhanced security
2. **State Parameter** - Required for CSRF protection
3. **Redirect URI Validation** - Must exactly match registered URIs
4. **Token Expiration** - Access tokens inherit the same expiration as manually generated ones
5. **Client Secret Storage** - Store as SHA-256 hash, never plaintext

---

## Claude.ai Configuration

After implementation, users will configure Claude.ai with:

| Field | Value |
|-------|-------|
| **Server Name** | `qmemory` |
| **Server URL** | `https://mem0.qusai.org/mcp/` |
| **OAuth Client ID** | `claude-ai` (public, documented) |
| **OAuth Client Secret** | Generated per-deployment, shown in Railway env vars |

---

## Estimated Effort

| Task | Complexity | Time |
|------|------------|------|
| Task 1: Schema | Low | 15 min |
| Task 2: OAuth Routes | Medium | 45 min |
| Task 3: Consent Page | Low | 20 min |
| Task 4: Register Client | Low | 10 min |
| Task 5: Mount Routes | Low | 5 min |
| Task 6: Tests | Medium | 30 min |
| Task 7: Update Connect | Low | 10 min |
| Task 8: Deploy & Test | Medium | 30 min |
| **Total** | | **~3 hours** |
