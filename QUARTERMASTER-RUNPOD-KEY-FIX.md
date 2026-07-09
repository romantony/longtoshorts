# `E2E-shorts-deliver`: RUNPOD_API_KEY → Secrets Manager

Handoff note for whoever owns the quartermaster/StoryStudio Lambda source repo
(the one whose `docs/quartermaster/storystudio-shorts-longform-integration.md`
this repo's `STORYSTUDIO-INTEGRATION.md` is the source of truth for). This
documents a live production fix applied directly to the deployed
`E2E-shorts-deliver` Lambda — **the source repo still needs this patch
ported in**, or the next deploy from there will silently revert it.

---

## 1. What was broken

`E2E-shorts-deliver` (account `929075264324`, `us-east-1`) is the Lambda that
calls the `shorts-longform` RunPod endpoint (this repo). Its code read the
bearer token from a plain env var:

```python
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
...
if not RUNPOD_API_KEY:
    raise RuntimeError("RUNPOD_API_KEY is not configured")
```

Checked the live config (`aws lambda get-function-configuration`) —
`RUNPOD_API_KEY` was **not set**. Only `RUNPOD_SHORTS_LONGFORM_ENDPOINT_ID`
was present. Every invocation was failing immediately at that check, before
even reaching request validation.

Scanned all ~70 `E2E-*`/shorts Lambdas in the account for any RunPod-related
env vars — `E2E-shorts-deliver` is the **only** one that talks to RunPod
directly, so this is the only Lambda affected.

## 2. Where the token actually lives

AWS Secrets Manager, `us-east-1`:

| | |
|---|---|
| Secret ID | `quartermaster/runpod-api-key` |
| ARN | `arn:aws:secretsmanager:us-east-1:929075264324:secret:quartermaster/runpod-api-key-pT4dTx` |
| Format | plain string (not JSON), the raw `rpa_...` bearer token |

`E2E-shorts-deliver`'s execution role (`E2E-Lambda-Execution-Role`, shared
across all `E2E-*` Lambdas) already has the managed policy
`SecretsManagerReadWrite` attached — no new IAM grant was needed to read
this secret. (Worth tightening later to a scoped inline policy like the
existing `ReadHuggingFaceSecret` pattern on the same role, resource-limited
to just this one secret ARN — not done here since it wasn't blocking.)

## 3. Fix applied (deployed directly via `aws lambda update-function-code`)

Resolution order: explicit `RUNPOD_API_KEY` env var (back-compat, still
wins if set) → Secrets Manager secret `RUNPOD_API_KEY_SECRET_ID` (default
`quartermaster/runpod-api-key`, overridable), cached per warm container so
each container only pays the Secrets Manager round-trip once.

```python
RUNPOD_API_KEY_SECRET_ID = os.environ.get("RUNPOD_API_KEY_SECRET_ID", "quartermaster/runpod-api-key")
RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_SHORTS_LONGFORM_ENDPOINT_ID", "u3bvq5juben8ri")
RUNPOD_BASE = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"

_runpod_api_key_cache = os.environ.get("RUNPOD_API_KEY", "")


def _resolve_runpod_api_key() -> str:
    """RUNPOD_API_KEY env var wins if set; otherwise pull the shared bearer token
    from Secrets Manager once per warm container and cache it."""
    global _runpod_api_key_cache
    if _runpod_api_key_cache:
        return _runpod_api_key_cache
    import boto3

    client = boto3.client("secretsmanager")
    _runpod_api_key_cache = client.get_secret_value(SecretId=RUNPOD_API_KEY_SECRET_ID)["SecretString"].strip()
    return _runpod_api_key_cache
```

Two call sites updated:

- `_request()`'s header build: `"Authorization": f"Bearer {_resolve_runpod_api_key()}"` (was the module-level `RUNPOD_API_KEY` constant).
- `lambda_handler()`'s fail-fast check at the top:

```python
try:
    _resolve_runpod_api_key()
except Exception as exc:
    raise RuntimeError(
        f"RUNPOD_API_KEY is not set and Secrets Manager lookup for "
        f"{RUNPOD_API_KEY_SECRET_ID!r} failed: {exc}"
    ) from exc
```

Docstring's "Environment variables" block updated to reflect that
`RUNPOD_API_KEY` is now optional and `RUNPOD_API_KEY_SECRET_ID` exists as an
override knob.

## 4. Verification

- Extracted the live deployed code (`aws lambda get-function` → download
  `Code.Location` → unzip), patched it, `ast.parse`'d it, and ran
  `_resolve_runpod_api_key()` directly against the real secret — resolved a
  50-char `rpa_...`-prefixed token successfully.
- Deployed via `aws lambda update-function-code --function-name
  E2E-shorts-deliver --zip-file fileb://...`. `LastUpdateStatus` reached
  `Successful`.
- Invoked the live Lambda with a minimal (intentionally invalid) payload.
  CloudWatch log showed it now gets **past** the key resolution (logged
  `Found credentials in environment variables` — the boto3 Secrets Manager
  call) and fails only on the next, unrelated validation step
  (`projectId is required in event payload`). Before the fix it died at
  `RUNPOD_API_KEY is not configured` before reaching that point at all.

## 5. Action needed on your side

Port the change in §3 into your source repo's `lambda_function.py` for
`E2E-shorts-deliver` (the deployed code was patched directly via the AWS
CLI, not through your normal deploy path — there is no PR/commit backing
this in your repo yet). If your CI/CD redeploys this function from source,
it will overwrite the live fix with the old broken version until this is
merged in.
