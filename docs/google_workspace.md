# Google Workspace (OAuth + remote MCP)

Nova’s Calendar, Gmail, and Drive tools call Google’s **remote MCP** endpoints
(`calendarmcp` / `gmailmcp` / `drivemcp`) with a user OAuth token. This page is
the operator checklist for Google Cloud Console enablement and local auth.

Secrets stay in `.env` and `runtime/google_oauth/tokens.json` (both gitignored).
**Never commit client secrets, refresh tokens, or live probe scripts that embed
account IDs.**

---

## What CI does (and does not)

| Layer | Hits Google? | Credentials |
|-------|--------------|-------------|
| GitHub Actions (`scripts/run_tests.sh fast` / `all-local`) | **No** | None — fakes `ya29.fake` + in-memory MCP mocks |
| `@pytest.mark.live` tests | Only if you run them locally with keys | Your `.env` (deselected in CI) |
| Manual `scripts/google_mcp_auth.py` / demo | Yes | Your local `.env` + token file |

CI never loads `.env` from the repo (it is not checked in) and never runs the
interactive smoke we use on a laptop. Unit tests inject fake MCP callers; they
do not call `*.googleapis.com`.

---

## 1. Google Cloud project

1. Open [Google Cloud Console](https://console.cloud.google.com/) and select (or
   create) a project. Note the **Project ID** (e.g. `my-nova-project`).
2. Put it in `.env`:

```bash
GOOGLE_CLOUD_PROJECT=my-nova-project
```

Nova sends this as `x-goog-user-project` on MCP HTTP calls.

---

## 2. Enable APIs / services

In **APIs & Services → Library**, enable at least:

| Service | Why |
|---------|-----|
| **Google Calendar API** | Calendar data behind MCP / legacy REST |
| **Gmail API** | Mail behind Gmail MCP |
| **Google Drive API** | Drive behind Drive MCP |
| **People API** (optional) | If you use People MCP later |

For **Workspace remote MCP**, you must be in Google’s Developer Preview
program — apply here:

- [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview)

Without preview access, Calendar/Gmail/Drive REST can still work for other
clients, but Nova’s MCP `tools/call` path to `*mcp.googleapis.com` will not.

After you’re accepted (Google emails when features are ready):

1. Confirm the Cloud project used in `GOOGLE_CLOUD_PROJECT` is the one enrolled
   in the preview.
2. Ensure MCP hosts respond for your project (probe with
   `uv run python scripts/google_mcp_auth.py` after OAuth — it lists Calendar
   MCP tools).
3. Grant your user (or the service identity used for demos) IAM ability to use
   MCP tools where Google documents it (historically `roles/mcp.toolUser` or
   the current equivalent in the MCP docs). Re-check Google’s MCP setup page if
   the role name changes.

Without MCP `tools/call` entitlement, `tools/list` may work while calls fail.
Nova tools need **successful `tools/call`**.

---

## 3. OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **External** (or Internal if Workspace-only and you have that
   option).
3. App name / support email: your choice.
4. **Scopes** — add (match `GOOGLE_WORKSPACE_SCOPES` in
   `nova/tools/mcp/oauth.py`):

   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
   - `https://www.googleapis.com/auth/calendar.events.freebusy`
   - `https://www.googleapis.com/auth/calendar.events` (create / delete)
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
   - `https://www.googleapis.com/auth/drive.file` (create folders Nova makes)

5. Add **test users** (your work account) while the app is in Testing.
6. Publish only if you intentionally want non-test users.

---

## 4. OAuth client (Web application)

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Authorized redirect URIs — add **exactly**:

   ```text
   http://127.0.0.1:8765/oauth/callback
   ```

   (Default in Nova. Override only if you also set `GOOGLE_OAUTH_REDIRECT_URI`.)

4. Copy **Client ID** and **Client secret** into `.env`:

```bash
GOOGLE_OAUTH_CLIENT_ID=....apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=...
# optional overrides:
# GOOGLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8765/oauth/callback
# GOOGLE_OAUTH_TOKEN_PATH=runtime/google_oauth/tokens.json
```

---

## 5. Local login (once per machine / scope change)

```bash
# Prefer paste URL into work Chrome (correct Google account):
uv run python scripts/google_mcp_auth.py

# Or connect from the tool UI Settings → Connect with Google
uv run python scripts/run_demo.py
# open http://127.0.0.1:8000/
```

Tokens write to `runtime/google_oauth/tokens.json` (mode `0600`). After changing
scopes in Console, **Disconnect / delete the token file and re-auth** so the new
scopes are granted.

---

## 6. Verify MCP (manual, not CI)

With tokens present:

```bash
uv run python scripts/google_mcp_auth.py
```

Expect Calendar MCP `tools/list` to print tool names (`list_events`,
`create_event`, …). Then from the demo, try “what’s on my calendar tomorrow”,
“check my email”, “list my Drive files”.

| Symptom | Likely fix |
|---------|------------|
| `google_oauth_not_configured` | Missing client id/secret in `.env` |
| `google_oauth_not_authenticated` | Run auth script / Connect in UI |
| `tools/list` OK, `tools/call` denied | MCP program / IAM not ready for this project |
| 403 insufficient scopes | Re-auth after adding scopes on consent screen |
| Wrong Google account | Use work Chrome profile; delete token file and login again |

---

## Nova mapping (reference)

| Nova tool | MCP host | MCP tool(s) |
|-----------|----------|-------------|
| `check_calendar` | `calendarmcp.googleapis.com` | `list_events` |
| `create_calendar_event` | same | `create_event` |
| `delete_calendar_event` | same | `delete_event` (+ list for title match) |
| `check_email` | `gmailmcp.googleapis.com` | `search_threads`, `get_message` |
| `list_drive_files` | `drivemcp.googleapis.com` | `list_recent_files` / `search_files` |
| `create_drive_folder` | same | `create_file` (folder mime) |

Implementation: `nova/tools/mcp/{client,oauth,runtime,calendar,gmail,drive}.py`.
