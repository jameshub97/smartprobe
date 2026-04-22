# CLI Notes

## Image Pull Debugger — `docker pull` step fails

### Symptom
```
$ docker pull playwright-agent:latest
[FAIL] Error response from daemon: pull access denied for playwright-agent,
repository does not exist or may require 'docker login'
```

### Root cause
The batch (pull → tag → push) strips the `host.docker.internal:5050/` prefix and
calls `docker pull playwright-agent:latest` first. Docker tries Docker Hub because
there is no registry prefix — but `playwright-agent` is a **locally-built image**
that was never pushed to any public registry.

### Fix
Build the image locally first, then run the batch:

```bash
docker build -t playwright-agent:latest .
```

Once the image exists in the local Docker daemon the pull step is skipped and
the tag + push steps succeed.

### Fix applied
`_build_push_steps()` in `menus/image_pull.py` now calls `_image_exists_locally(name)`
(`docker images -q <name>`) before emitting a pull step. If the image is already in
the local Docker daemon the pull step is skipped, so the batch goes straight to
tag → push.
---

## Image Pull Debugger — `docker push` fails with HTTPS/HTTP mismatch

### Symptom
```
$ docker push host.docker.internal:5050/playwright-agent:latest
[FAIL] Get "https://host.docker.internal:5050/v2/": http: server gave HTTP response to HTTPS client
```

### Root cause
The local registry runs plain **HTTP**, but Docker defaults to HTTPS for any registry
that isn't `localhost`. The daemon tries TLS, the registry responds with HTTP, and
Docker rejects it.

### Fix
Add the registry to Docker's insecure registries list:

1. Open **Docker Desktop → Settings → Docker Engine**
2. Add to the JSON:
   ```json
   "insecure-registries": ["host.docker.internal:5050"]
   ```
3. Click **Apply & Restart**

After the restart `docker push` uses plain HTTP for that host.

### Fix applied in CLI
`run_push_commands()` in `menus/image_pull.py` now detects this specific error string
and prints the Docker Desktop fix inline before stopping the batch.

---

## Image Pull Debugger — registry-mirror 500 (image not pushed yet)

### Symptom
```
failed to resolve reference "host.docker.internal:5050/playwright-agent:latest":
unexpected status from HEAD request to
http://registry-mirror:1273/v2/playwright-agent/manifests/latest?ns=host.docker.internal%3A5050:
500 Internal Server Error
```

### Root cause
containerd is configured to proxy all pulls through `registry-mirror:1273`. That
proxy forwards to `host.docker.internal:5050`. The proxy returns 500 because the
image **does not exist in the local registry yet** — either the push never ran or
it failed (e.g. the HTTPS/HTTP error above).

### Fix
1. Ensure `host.docker.internal:5050` is in Docker Desktop insecure registries (see above)
2. Re-run the push batch in the Image Pull Debugger — tag → push will now succeed
3. Once the image is in the local registry the mirror can proxy it and pods will start

### Fix applied in CLI
`render_image_pull_diagnosis()` in `menus/image_pull.py` detects the 500 mirror
pattern and prints the exact fix steps inside the diagnosis box.