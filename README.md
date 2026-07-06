# Gemini API Quota Manager

Manage **Gemini API** quotas on Google Cloud via the [Cloud Quotas API](https://cloud.google.com/docs/quotas/api-overview).

Repository: **`gemini-quota-manager`** — matches the script name `gemini_quota_manager.py`. Full design: [docs/SPEC.md](docs/SPEC.md).

Default target: **Request limit per model per day for a project in the paid tier** on `generativelanguage.googleapis.com`.

| Mode | Description |
|---|---|
| `list` | Show matching quotas, reported limits, and **Policy** (`cap` / `leave` / `disable`) |
| `update` | Set `preferredValue` on matching quotas (dry-run by default) |

---

## Quick start

```bash
# 1. Complete GCP setup below (APIs, IAM, auth)
# 2. List quotas
./gemini_quota_manager.py --project YOUR_PROJECT_ID list

# 3. Dry-run an update (no changes submitted)
./gemini_quota_manager.py --project YOUR_PROJECT_ID update --ack-decrease-risks

# 4. Apply
./gemini_quota_manager.py --project YOUR_PROJECT_ID update --ack-decrease-risks --apply
```

> **Argument order:** `--project` must come **before** `list` or `update`.

---

## Local prerequisites

| Tool | Purpose |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Runs the script and installs Python deps automatically |
| [gcloud CLI](https://cloud.google.com/sdk/docs/install) | Auth and enabling GCP APIs |
| [task](https://taskfile.dev/) (optional) | `task lint`, `task test` for development |

```bash
# Clone / enter the repo, then:
chmod +x gemini_quota_manager.py   # once, for ./gemini_quota_manager.py

# Dev tooling (optional)
task init
```

The script is self-contained (PEP 723 inline deps). You do **not** need to activate a venv to run it — `uv` handles that.

Alternative invocation:

```bash
uv run gemini_quota_manager.py --project YOUR_PROJECT_ID list
```

---

## GCP setup

Replace `YOUR_PROJECT_ID` with your project (e.g. `gemini-api-80295`).

### 1. Select the project

```bash
gcloud config set project YOUR_PROJECT_ID
```

### 2. Enable required APIs

Both APIs must be enabled on the **same project** you pass to `--project`.

```bash
# Option A: pass project as argument
task setup -- YOUR_PROJECT_ID

# Option B: pass project via environment
GCP_PROJECT=YOUR_PROJECT_ID task setup

# Option C: use gcloud's active project (interactive confirmation prompt)
gcloud config set project YOUR_PROJECT_ID
task setup
```

After APIs are enabled, setup may ask to run `gcloud config set project` so your shell default matches (skipped if already set).

Equivalent `gcloud` command:

```bash
gcloud services enable \
  cloudquotas.googleapis.com \
  generativelanguage.googleapis.com \
  --project=YOUR_PROJECT_ID
```

| API | Why |
|---|---|
| `cloudquotas.googleapis.com` | **Required.** Programmatic list/update of quota preferences |
| `generativelanguage.googleapis.com` | Gemini API quotas live under this service |

**Console links** (same as above):

- [Enable Cloud Quotas API](https://console.cloud.google.com/apis/library/cloudquotas.googleapis.com?project=YOUR_PROJECT_ID)
- [Enable Generative Language API](https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com?project=YOUR_PROJECT_ID)

After enabling, wait **2–5 minutes** for propagation before retrying.

### 3. IAM permissions

Grant the identity that runs the script:

| Command | Minimum role |
|---|---|
| `list` | `roles/cloudquotas.viewer` |
| `update` | `roles/cloudquotas.admin` |

```bash
# Example: grant your user account viewer + admin on the project
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="user:you@example.com" \
  --role="roles/cloudquotas.admin"
```

For automation, use a service account with the same roles and set `GOOGLE_APPLICATION_CREDENTIALS`.

### 4. Authenticate (Application Default Credentials)

```bash
gcloud auth application-default login
```

The script sets the **quota project** from `--project` automatically (sends `x-goog-user-project`). You do not need `gcloud auth application-default set-quota-project` separately.

### 5. Verify setup

```bash
# APIs enabled?
gcloud services list --enabled --project=YOUR_PROJECT_ID \
  | grep -E 'cloudquotas|generativelanguage'

# Can you list quotas?
./gemini_quota_manager.py --project YOUR_PROJECT_ID list
```

View quotas in Console: [Generative Language API → Quotas](https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com/quotas?project=YOUR_PROJECT_ID)

---

## Usage

### List matching quotas

Default filter: **Request limit per model per day for a project in the paid tier** (matches paid tier 2/3 + priority variants; excludes Predict quotas). Default model dimension filter: **`gemini`** (~144 rows: 36 models × 4 quota variants on a typical project).

```bash
./gemini_quota_manager.py --project YOUR_PROJECT_ID list
./gemini_quota_manager.py --project YOUR_PROJECT_ID list --value 1000   # Policy column uses this cap
```

List output columns: **Quota ID**, **Display Name**, **Model dim.**, **Reported value** (`-1 (unlimited)` when uncapped), **Policy**.

### Update quotas (dry-run first)

```bash
# Validate only (validateOnly=true) — recommended first step
./gemini_quota_manager.py --project YOUR_PROJECT_ID update --ack-decrease-risks

# Submit changes
./gemini_quota_manager.py --project YOUR_PROJECT_ID update \
  --ack-decrease-risks \
  --apply
```

On `update`, the script first **lists existing QuotaPreferences** and reuses the server’s resource name when one already exists for the same `(service, quotaId, dimensions)`. That avoids `400 … already exist` when preferences were created in Console or a prior run under a different ID.

Common flags:

| Flag | Default | Description |
|---|---|---|
| `--value` | `1000` | Cap for enabled models (`list` Policy column and `update` target) |
| `--apply` | off | Submit changes; without it, dry-run only (`validateOnly=true`) |
| `--ack-decrease-risks` | required on `update` | Acknowledges quota decrease safety checks |
| `--models` | `gemini` | Model dimension must contain this substring |
| `--exclude-models` | none | Denylist applied before `--models` |
| `--filters` | paid-tier daily limit | Display-name substrings (AND logic) |
| `--skip-unknown-values` | off | Skip rows whose reported value is not numeric |
| `--rps` / `--burst` / `--max-retries` | `4` / auto / `5` | Rate limit and retry for API calls |

**Model policies** (evaluated per model dimension; project-wide rows without a model dim are always capped):

| Policy | When | Update target |
|---|---|---|
| **disable → 0** | Matches any disable rule below | quota `0` |
| **cap → N** | Enabled and reported value ≥ `--value` (or unlimited) | `--value` (default `1000`) |
| **leave → N** | Enabled and reported value &lt; `--value` | unchanged (skipped on update) |

**Disable rules** (any match → `disable → 0`):

1. **Explicit denylist** (`DISABLED_MODELS` in source):
   - `gemini-2.5-flash-native-audio-dialog`
   - `gemini-2.5-flash-preview-image`
   - `gemini-2.5-pro-1p-freebie`
   - `gemini-3.1-flash-image`
   - `gemini-3-pro-image`
2. **Suffix** (case-insensitive): `tts`, `exp`, `lite`, `live`
3. **Substring**: `exp-*` (`exp-` followed by more characters)
4. **Below minimum version**: older than **gemini-2.5-flash** (tier ≥ flash required; `gemini-3-flash` / `gemini-3-pro` count as 3.0)

**Typical enabled models** (subject to your project’s quota rows): `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-3-flash`, `gemini-3-pro`, `gemini-3.1-pro`, `gemini-3.5-flash`, plus project-wide rows.

**Skip behavior on `update`:**

- Reported value already equals target → **skipped**
- Reported value below `--value` (default 1000) → **skipped** (left unchanged; quotas are never raised)
- Unlimited (`-1` / `Unlimited`) → **always updated** (capped to `--value`, or `0` if disabled)
- **Ctrl+C** during `update` → partial summary, exit `130`

---

## Troubleshooting

### Cloud Quotas API disabled (`SERVICE_DISABLED`)

```
Cloud Quotas API has not been used in project … before or it is disabled.
```

**Fix:**

```bash
gcloud services enable cloudquotas.googleapis.com --project=YOUR_PROJECT_ID
```

Or open: [Cloud Quotas API activation](https://console.cloud.google.com/apis/library/cloudquotas.googleapis.com?project=YOUR_PROJECT_ID)

Wait a few minutes, then retry.

### Quota project not set (ADC)

```
The cloudquotas.googleapis.com API requires a quota project, which is not set by default.
```

**Fix:** Use a recent version of the script — it sets the quota project from `--project`. Ensure you pass:

```bash
./gemini_quota_manager.py --project YOUR_PROJECT_ID list
```

### `403 PERMISSION_DENIED` (not SERVICE_DISABLED)

- Confirm IAM: `roles/cloudquotas.viewer` (list) or `roles/cloudquotas.admin` (update)
- Re-auth: `gcloud auth application-default login`
- Confirm `--project` is the project where APIs are enabled

### Empty list results

- Broaden filters: `--filters "gemini"`
- Compare with [Console quotas page](https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com/quotas?project=YOUR_PROJECT_ID)
- Confirm you're on `generativelanguage.googleapis.com` (default `--service`)

### Update accepted but value unchanged

Quota preferences are **requests** — Google may queue them for review. Check the [Generative Language API quotas page](https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com/quotas?project=YOUR_PROJECT_ID).

### `400` on update (preference already exists)

If Console (or a prior run) already created a QuotaPreference for that model, the script must PATCH the **existing** resource name — not create a new one. Current versions list preferences before updating and reuse the match automatically.

### `400 INVALID_ARGUMENT` (`validateOnly` / `allowMissing` on POST)

Updates use **`PATCH`** on `quotaPreferences/{id}` (`allowMissing=true` only when creating a new preference). Do not use POST create.

### Unexpected SKIP

- Reported value below `--value` → intentional **leave** policy; run `list` and check the Policy column
- Disabled model → `disable → 0` in Policy column

---

## Development

```bash
task init    # install ruff + pytest
task setup   # enable GCP APIs (GCP_PROJECT=your-proj task setup)
task lint    # ruff check + format
task test    # pytest
task format  # auto-fix formatting
```

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Fatal error (auth, API, missing flags) |
| `2` | Partial failure (some QuotaPreference writes failed) |
| `130` | Interrupted (Ctrl+C during `update`) |
