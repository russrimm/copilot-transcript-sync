# Copilot Studio transcript sync

Collects Microsoft Copilot Studio conversation transcripts from **every Power Platform
environment in a tenant** and lands them in a single Azure Data Explorer database on a
schedule.

Dataverse bulk-deletes `conversationtranscript` rows after 30 days by default. This
pipeline exists to build a durable, queryable archive that outlives that window, with
transcripts from all environments in one place.

---

## How it works

```
Timer (every 30 min)
      │
      ▼
Azure Function (Python 3.13, Flex Consumption)
      │  user-assigned managed identity
      │      ├──────────────► Azure Data Explorer   (Ingestor)
      │      ├──────────────► Table Storage         (watermarks)
      │      │
      │      └── workload identity federation ──► Entra app registration
      │                                                  │
      │                          ┌───────────────────────┴───────────────────┐
      │                          ▼                                           ▼
      │             BAP admin API (app-only)                    Dataverse Web API
      │             list every environment                      per environment
      │             provision application users                 read conversationtranscript
      ▼
CopilotTranscriptRaw ──► CopilotTranscript (dedup MV) ──► CopilotTranscriptTurn()
                                                      └─► CopilotConversationPair()
```

### No secrets anywhere

The Function App holds a user-assigned managed identity and nothing else. That identity
is federated onto an Entra app registration, so Power Platform is reached by exchanging
a managed identity token for an app token — never a client secret.

The app registration exists because Power Platform requires one: tenant-wide environment
listing only works for an app registered as a *Power Platform management application*,
and each environment's Dataverse application user is bound to that app's client ID.

---

## Design decisions worth knowing

### Environment discovery uses the legacy BAP admin endpoint, on purpose

`GET https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments`
is currently the only documented endpoint that lists **all** environments in a tenant to
an **app-only** caller.

The newer `api.powerplatform.com` surface exposes only *List Environments **For User***,
still marked preview, and Microsoft states plainly that
["Power Platform API uses delegated permissions only at this time"][pp-auth-v2]. Using it
would mean storing an administrator's refresh token and renewing it forever — exactly the
pattern this design avoids.

Both endpoints carry pre-release banners. Neither is unambiguously GA for tenant-wide
app-only discovery. That is a real gap, not an oversight.

### Change tracking is deliberately not used

Dataverse offers `Prefer: odata.track-changes` with delta links. It is the wrong tool here:

| Problem | Consequence |
|---|---|
| Delta links **propagate deletes** | The 30-day Dataverse bulk-delete job would erase the archive this pipeline exists to build |
| `$filter`, `$orderby`, `$top`, `$expand` are [rejected][change-tracking] with the header | No way to bound or resume a run |
| Delta tokens expire after a default of 7 days | Any longer outage silently forces a full reseed |

Instead the job keeps a `createdon` high-water mark per environment, replays a
configurable overlap window on each run, and deduplicates in ADX. The same trap applies to
the Azure Synapse Link route Microsoft otherwise recommends — it mirrors deletes unless you
explicitly choose append-only mode.

### Transcripts over 1 MB are split across rows

`content` is capped at 1,048,576 characters. Larger conversations become multiple Dataverse
rows sharing `Name` and `ConversationStartTime`, distinguished by `Metadata.BatchId`.
`CopilotConversationPair()` orders by `BatchId` before pairing so a conversation split
across rows still reassembles correctly.

### `from.role` is numeric and inverted

In transcript content, `from.role` is **`0` for the agent and `1` for the user** — not the
`"user"`/`"bot"` strings the Bot Framework schema uses elsewhere. `CopilotTranscriptTurn()`
normalizes both forms into a `Speaker` column. Similarly, `timestamp` is epoch seconds
(confirmed against live data), though ISO 8601 and epoch milliseconds also occur; all three
are handled.

### `Content` has three encodings, and two of them fail silently

Real transcripts store `content` in at least three shapes:

1. a bare array of activities
2. `{"activities": [ ... ]}` — the common case in current schema versions
3. double-encoded `{"text": "<json string>"}`, activities nested as a JSON string

Microsoft documents the activity *fields* but never the top-level shape. Handling only one
form does not raise an error — it silently yields zero activities, which reads as an empty
conversation rather than a parsing bug. `CopilotTranscriptTurn()` accepts all three.

---

## Prerequisites

1. **Environment types.** Transcripts are never written to Dataverse for **Developer**
   environments, **Dataverse for Teams** environments, or **Microsoft 365 Copilot agents**.
   The job skips Developer and Teams automatically. Agents must live in Sandbox or
   Production environments with a Dataverse database.

2. **Transcript saving must be on**, per environment: Power Platform admin center →
   **Manage → Environments → [env] → Settings → Product → Features → Copilot Studio agents**
   → *"Allow conversation transcripts and their associated metadata to be saved in
   Dataverse"*.

   To enable it in bulk, publish the **Accessing transcripts from conversations in Copilot
   Studio agents** environment group rule. Note that [environment groups only accept managed
   environments][env-groups], so this is not available for every tenant. Configuring the
   rule without selecting **Publish rules** enables nothing.

3. **Transcripts lag.** A transcript is written after 30 minutes of conversation inactivity
   (3 minutes after *End Conversation* on the Telephony channel). Expect up to ~30 minutes
   before a finished conversation appears.

4. **Roles for setup.** Creating the app registration needs Application Administrator (or
   equivalent). Registering it as a management application needs Power Platform
   Administrator or Global Administrator.

---

## Deploy

### 1. Create the app registration

```powershell
./scripts/01-create-app-registration.ps1
```

Outputs the client ID. No secret is created.

### 2. Deploy the infrastructure

```powershell
az group create --name rg-copilot-transcripts --location eastus

az deployment group create `
  --resource-group rg-copilot-transcripts `
  --template-file infra/main.bicep `
  --parameters powerPlatformAppClientId=<client-id-from-step-1> `
               adxAdminPrincipalId=$(az ad signed-in-user show --query id -o tsv)
```

Note the `identityPrincipalId` output — step 3 needs it.

### 3. Federate the identity and register with Power Platform

```powershell
./scripts/02-federate-and-register.ps1 `
  -AppClientId <client-id> `
  -ManagedIdentityPrincipalId <identityPrincipalId-from-step-2>
```

> The federated credential's `subject` must be the managed identity's **Object (principal)
> ID**, not its client ID. Entra accepts a wrong value without error; the failure only
> appears later as a failed token exchange.

### 4. Create the ADX schema

```powershell
python scripts/deploy_kql.py `
  --cluster <adxClusterUri-from-step-2> `
  --database CopilotTranscripts
```

### 5. Publish the Function

```powershell
func azure functionapp publish <functionAppName-from-step-2>
```

> **If your subscription forces storage private, use a local build instead.**
>
> The default publish performs a **remote build**, and its Kudu validation step fails when
> the deployment storage account has `publicNetworkAccess = Disabled`:
>
> ```
> [Kudu-ValidationStep] starting.
> [StorageAccessibleCheck] ... 403 (This request is not authorized to perform this operation.)
> InaccessibleStorageException: Failed to access storage account for deployment
> ```
>
> Vendor the Linux dependencies yourself and publish without a remote build. This works —
> the package upload itself succeeds against the private storage account, only the
> remote-build validation fails:
>
> ```powershell
> pip install `
>   --target=".python_packages/lib/site-packages" `
>   --platform manylinux2014_x86_64 `
>   --only-binary=:all: `
>   --python-version 3.13 `
>   -r requirements.txt
>
> func azure functionapp publish <functionAppName> --python --no-build
> ```
>
> Two things worth knowing:
>
> - **`--no-build` on its own deploys an app with zero functions.** It ships no dependencies,
>   so the worker cannot import `azure.functions` and indexing silently yields nothing. The
>   publish still reports success. Populate `.python_packages` first.
> - **Moving your build agent into the virtual network does not help.** Every first-party
>   tool POSTs the package to the SCM `/api/publish` endpoint, and the platform writes it to
>   storage — so the client's network location is irrelevant to the leg that was failing.
>
> Do not relax the policy to work around this, particularly if it is inherited from a
> management group.

### 6. Trigger the first run

```powershell
az functionapp function keys list -g rg-copilot-transcripts -n <functionAppName> --function-name TranscriptSyncManual
curl -X POST "https://<functionAppName>.azurewebsites.net/api/sync?code=<key>"
```

The response reports per-environment row counts and any environments that failed.

---

## Querying

```kusto
// Recent conversations, paired prompt and response
CopilotConversationPair()
| where ConversationStartTime > ago(1d)
| project ConversationStartTime, EnvironmentName, BotName, UserPrompt, AgentResponse, ResponseLatency
| order by ConversationStartTime desc

// Which environments are reporting
CopilotTranscriptCoverage(7d)

// Every activity in one conversation, in order
CopilotTranscriptTurn()
| where ConversationId == "<conversation-id>"
| order by BatchId asc, ActivityIndex asc
| project ActivityTimestamp, Speaker, ActivityType, Text
```

---

## Security posture

The Function's own footprint is minimal: a managed identity with **Ingestor** on one ADX
database, data-plane roles on one storage account, and no secrets.

**The Dataverse side is the weak point, and it is a deliberate tradeoff.** The
`addAppUser` endpoint that provisions application users across environments
[always grants **System Administrator**][add-app-user] and offers no way to choose a role.
That is how the environments are self-healing when a new one appears — and it is far more
privilege than reading two tables requires. The job only ever issues `GET` requests against
`conversationtranscript`.

To tighten this, set `AUTO_PROVISION_APP_USER=false` and provision application users
yourself with a custom role granting Read at Organization scope on `bot` and
`conversationtranscript` only:

```powershell
pac admin assign-user `
  --environment <environment-id> `
  --user <app-client-id> `
  --role "Copilot Transcript Reader" `
  --application-user
```

The custom role must already exist in each environment; ship it in a small solution.

Also note: transcripts contain **conversation content**, which routinely includes personal
data. Treat the ADX database as a sensitive data store — restrict Viewer, and set retention
to match your actual obligation rather than leaving the 730-day default.

---

## Operating notes

**Never run `.clear materialized-view CopilotTranscript data`.** Clearing empties the view
and it does **not** backfill — the view then only materializes ingestion that arrives
afterwards, so every historical transcript silently disappears from every query built on it.
The raw table is unaffected, so recovery is to drop the view and let `scripts/deploy_kql.py`
recreate it with `backfill=true`.

Deleting rows from `CopilotTranscriptRaw` does not remove them from `CopilotTranscript`
until the view is rebuilt, for the same reason.

---

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `SYNC_SCHEDULE` | `0 */30 * * * *` | Timer NCRONTAB. Tighter than 30 min adds request pressure without adding freshness. |
| `INITIAL_BACKFILL_DAYS` | `30` | History pulled the first time an environment is seen. Beyond ~30 days there is nothing left in Dataverse to pull. |
| `WATERMARK_LOOKBACK_MINUTES` | `120` | Overlap replayed before the stored watermark. Duplicates are removed by the ADX view. |
| `DATAVERSE_PAGE_SIZE` | `25` | `odata.maxpagesize`. Each row can carry 1 MB, so large pages mean large responses. |
| `AUTO_PROVISION_APP_USER` | `true` | Whether to call `addAppUser` for newly discovered environments. |
| `EXCLUDED_ENVIRONMENT_TYPES` | `Developer,Teams` | Additional SKUs to skip. Developer and Teams are always skipped regardless. |

Throttling is handled per Microsoft's [service protection limits][api-limits]: 429 responses
honor `Retry-After` exactly, because ignoring it causes the penalty window to be extended.

---

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q

Copy-Item local.settings.json.example local.settings.json   # then fill in values
func start
```

Leaving `UAMI_CLIENT_ID` blank locally makes the code fall back to `DefaultAzureCredential`,
so you authenticate as yourself via `az login`. That exercises discovery and extraction, but
**not** the application-user path — a local run behaves as your user account, not as the app.

> **Watch out:** if `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` are set in your shell,
> `DefaultAzureCredential` resolves `EnvironmentCredential` *before* your `az login` session
> and silently authenticates as that service principal instead. `scripts/deploy_kql.py`
> therefore defaults to `AzureCliCredential`; pass `--credential default` to opt out.

### Smoke scripts

These talk to the real tenant and cluster, and are not part of `pytest`:

| Script | What it proves |
|---|---|
| `scripts/smoke_discovery.py` | Tenant-wide app-only environment listing, and the eligibility filter |
| `scripts/smoke_extract.py` | Dataverse extraction and ADX ingestion across every eligible environment |
| `scripts/smoke_pairing.py` | `CopilotConversationPair()` against a synthetic conversation, then cleans up after itself |

---

## Known gaps

- Both documented environment-listing endpoints carry pre-release banners.
- `New-PowerAppManagementApp` cannot be run by a service principal for itself; the
  bootstrap genuinely requires an interactive administrator once. The REST equivalent
  (`PUT .../adminApplications/{clientId}`) accepts an Azure CLI delegated token, which
  `scripts/02-federate-and-register.ps1` uses to avoid the PowerShell module.
- Microsoft documents no end-to-end "managed identity → Dataverse Web API" flow. This
  project uses the documented workload identity federation path instead, which is GA.
- Whether change tracking is enabled by default on `conversationtranscript` is not
  documented. It does not matter here, since change tracking is not used.
- **Remote build fails when the deployment storage account is private.** A local build
  (`--no-build` with vendored dependencies) succeeds against the same account, so this is
  specific to the remote-build validation step rather than to storage access as such.
  Microsoft documents no supported end-to-end procedure for deploying Flex Consumption with
  `publicNetworkAccess = Disabled`, even though its own secured samples ship that topology.
  See step 5.

## Verification status

Verified end to end against a live tenant, with the Function running in Azure:

| Area | Result |
|---|---|
| Tenant-wide app-only environment discovery | 12 environments with Dataverse, 7 eligible |
| Developer / Teams filtering | Correctly skipped |
| Workload identity federation at runtime | Function reached Power Platform with no secret |
| Dataverse transcript extraction | 8 transcripts read across environments |
| Application user auto-provisioning | Ran clean across all eligible environments |
| Transport-error retry and per-environment isolation | Exercised by real TLS resets during local runs |
| ADX ingestion and JSON mapping | All rows landed, zero parse errors |
| `CopilotTranscript` dedup materialized view | Correct |
| `CopilotTranscriptTurn()` | Turns expanded; roles and epoch timestamps decoded correctly |
| `CopilotConversationPair()` | Verified with a synthetic conversation, including merging consecutive agent replies |
| Watermark persistence | Second run returned only the overlap window, proving the Table Storage round trip over a private endpoint |
| Overlap deduplication under repeated runs | 18 raw rows across three runs collapsed to 8 distinct transcripts in the view |
| Infrastructure | Deploys clean, including private networking |

The second sync run returning one row per environment rather than zero is expected: the
overlap window deliberately replays the newest transcript, and the deduplication view removes
the repeat. That is visible in the numbers above — the raw table accumulates replays while
`CopilotTranscript` stays at the true transcript count.

[pp-auth-v2]: https://learn.microsoft.com/en-us/power-platform/admin/programmability-authentication-v2
[change-tracking]: https://learn.microsoft.com/en-us/power-apps/developer/data-platform/use-change-tracking-synchronize-data-external-systems
[add-app-user]: https://learn.microsoft.com/en-us/power-platform/admin/create-dataverseapplicationuser
[api-limits]: https://learn.microsoft.com/en-us/power-apps/developer/data-platform/api-limits
[env-groups]: https://learn.microsoft.com/en-us/power-platform/admin/environment-groups
