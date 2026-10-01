# Pipedream Connect Guide — Using Third-Party Integrations

You have access to 3,000+ connected apps (Gmail, Google Calendar, GitHub, Notion,
HubSpot, Salesforce, Linear, Jira, and more) through the integrations gateway.
Use the `pdx` CLI to interact with them.

---

## When to use integrations

Use `pdx run` whenever a task involves a connected app — reading emails,
creating issues, updating calendar events, querying CRMs, etc. The gateway
handles authentication and tool dispatch automatically.

Use `pdx http` when you need to make a specific, known API call directly and
don't need the LLM to figure out which tool to use — it's faster and more
predictable than `pdx run` for well-defined operations.

Use the browser for anything without a gateway integration.

---

## Checking what's available

```bash
pdx status
# → {"ok": true, "user_id": "...", "unique_channel": "...", "gateway": "https://..."}
```

If `ok` is false, the gateway is not configured — fall back to the browser.

---

## Running a task with `pdx run`

Discover available actions, inspect the required props, then run:

```bash
# 1. List actions for an app
pdx actions github
# → {"ok": true, "count": 12, "data": [{"key": "github-create-issue", ...}, ...]}

# 2. Describe an action to see its props
pdx describe github-create-issue
# → {"ok": true, "key": "github-create-issue", "props": {"repoFullname": ..., "title": ..., ...}}

# 3. Run the action with the required props
pdx run github-create-issue --arg repoFullname=acme/repo --arg title="Bug fix"
# → {"ok": true, "action_key": "github-create-issue", "result": {...}}

# Or pass all props as JSON
pdx run github-create-issue --args '{"repoFullname": "acme/repo", "title": "Bug fix", "body": "Details"}'
```

---

## Raw HTTP proxy

Use `pdx http` to make a specific authenticated API call directly — no LLM
involved. The gateway resolves credentials from `x-ninja-user-id` +
`x-ninja-integration-channel-id` and proxies the request upstream.

```bash
# GET request
pdx http github GET https://api.github.com/user

# POST with JSON body
pdx http github POST https://api.github.com/repos/acme/repo/issues \
    --json '{"title": "Bug fix", "body": "Details here"}'

# GET with query parameters
pdx http gmail GET https://www.googleapis.com/gmail/v1/users/me/messages \
    --query maxResults=10 --query labelIds=INBOX

# POST with extra headers
pdx http hubspot POST https://api.hubapi.com/crm/v3/objects/contacts \
    --json '{"properties": {"email": "user@example.com"}}' \
    --header 'Content-Type:application/json' \
    --thread-id "$THREAD_TS"
```

Output envelope:

```json
{
  "ok": true,
  "app_slug": "github",
  "request": {"method": "GET", "url": "...", "headers": {}, "query": {}, "json": null},
  "response": {"status": 200, "headers": {...}, "body": {...}},
  "upstream_ok": true
}
```

`upstream_ok` reflects whether the upstream API returned a 2xx status. The
outer `ok` reflects whether the Pipedream Connect gateway itself succeeded.

---

## App not connected

If the user hasn't connected an app yet, the response will include a connect URL.
Post it to the user:

```bash
pdx connect-link
# → {"ok": true, "link": "https://..."}

python messaging/whatsapp/interface.py say "Connect your apps here (expires in 30 min): <link>"
```

---

## Error handling

All failures return:

```json
{ "ok": false, "error": "<description>" }
```

| Exit code | Meaning                                                      |
| --------- | ------------------------------------------------------------ |
| `1`       | Bad arguments                                                |
| `2`       | Configuration error — `pdx status` to diagnose               |
| `3`       | Runtime error — gateway unreachable or app returned an error |

If exit code is `3` and the error mentions a connect URL, the app isn't
connected — use `pdx connect-link` to get the user a fresh link.
