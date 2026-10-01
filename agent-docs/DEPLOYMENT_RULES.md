## Deployment Rules

Check your sandbox type: `python3 -c "import json; print(json.load(open('/dev/shm/sandbox_metadata.json')).get('sandbox_provider', 'shared'))"`

If `sandbox_provider` is `ninja-dedicated`, you are on a dedicated sandbox where users can access deployed services directly. When deploying or running any server/service:

1. Bind to **`0.0.0.0`** (all interfaces), **not** `localhost` or `127.0.0.1`.
   Example: `python3 -m http.server <PORT> --bind 0.0.0.0`, or `--host 0.0.0.0` for frameworks.
   Pick any free port; there is nothing special about any particular number
2. Run it in the background so it stays up (e.g. `nohup … &` or a supervisor entry).
3. Avoid ports already in use: 22, 5000, 6080, 8080, 9000, 9020, 9222.


## The public URL

Every bound port is reachable from outside at:

```
https://<PORT>-<THREAD_ID>.app.super.<PREFIX>myninja.ai
```

- `<THREAD_ID>` — `thread_id` from `/dev/shm/sandbox_metadata.json`
- `<PREFIX>` — `environment` from the same file, **omitted entirely when it is `prod`**
  (so `beta` → `app.super.betamyninja.ai`, prod → `app.super.myninja.ai`)

Build it in code rather than by hand:

```python
from core.metadata import load_sandbox_metadata

meta = load_sandbox_metadata()
stage = meta.get("environment", "")
prefix = stage if stage and stage != "prod" else ""
url = f"https://{port}-{meta['thread_id']}.app.super.{prefix}myninja.ai"
```

**Always give the user this URL** after starting a service — a `localhost` or
`0.0.0.0` address is useless to them.

Do not conclude from a failed `curl` that the port is not exposed. The ingress
redirects unauthenticated requests to `/app/unavailable?port=<PORT>`, and it does
this for working ports too. Only a logged-in browser can confirm reachability.
