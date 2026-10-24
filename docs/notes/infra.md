# Infra: worker in Azure, replay job, Easy Auth readiness

Checked: `az bicep build --file infra/main.bicep` passes with no warning; `azd provision --preview` (what-if, nothing applied) on `watershed-dev` with `FOUNDRY_ACCOUNT_ID` set plans: create `ca-worker-*` and `caj-replay-*`, modify `ca-web-*` (env vars, image kept), the other changes are pre-existing drift (API defaults). No Docker daemon was running, so the images were not built locally; `pip wheel .` packages `generator`, `winprob` and `worker` as the Dockerfile expects.

## What changed

| File | Change |
|---|---|
| `azure.yaml` | New `worker` service; both services build from the repository root (`docker.context: ../..`, relative to the project folder) |
| `.dockerignore` | Repository root: excludes `.git`, `.claude`, `.azure`, `.env*`, `.venv`, `docs`, `spikes`, `tests`, `infra`, `prep-*`, and `data/*` except `data/replay` |
| `src/worker/Dockerfile` | python:3.12-slim, `pip install ".[azure,agents]"`, copies `data/replay`, runs as a non-root user, loops `python -m worker --live --cosmos --writer foundry $WORKER_ARGS` |
| `infra/resources.bicep` | Worker and replay identities, worker Container App with a KEDA Service Bus rule, `replay` Container Apps Job, Service Bus Data Sender for the web identity, new web env vars, optional Easy Auth, Foundry role through a module; Container Apps API moved to 2025-01-01 (needed for the scale rule identity) |
| `infra/modules/foundry-role.bicep` | Role assignment on the Foundry account, deployed in `rg-watershed-foundry` |
| `infra/main.bicep`, `main.parameters.json` | New parameters from azd env vars: `SERVICE_WEB_IMAGE_NAME`, `SERVICE_WORKER_IMAGE_NAME`, `FOUNDRY_ACCOUNT_ID`, `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL`, `FOUNDRY_MODEL_FALLBACK`, `FOUNDRY_REASONING_EFFORT`, `WORKER_EXTRA_ARGS`, `WEB_AUTH_CLIENT_ID`; new outputs `AZURE_RESOURCE_GROUP`, `AZURE_WORKER_APP_NAME`, `AZURE_REPLAY_JOB_NAME` |

## Why KEDA and not a long-running worker

`listen` returns after an idle period. The worker runs as a Container App with no ingress, `minReplicas 0`, `maxReplicas 1` and an `azure-servicebus` scale rule on the `worker` subscription (`messageCount 1`), authenticated with the worker identity. When a replay starts, KEDA sees active messages (polled every 30 s) and starts one replica; inside it, a shell loop restarts `listen` 5 s after each idle exit, so the replica keeps consuming. KEDA stops the replica after the subscription has been empty for its cool-down (300 s). Cost when idle: zero. Without the loop, Container Apps would treat each exit as a crash and restart it with a growing back-off. `maxReplicas 1` keeps a single consumer: the match state lives in the process memory. `activeRevisionsMode: Single` is what Microsoft asks for with non-HTTP scale rules.

## Identities and roles

All assignments are Azure RBAC or Cosmos DB data-plane RBAC, no key and no connection string. Every app gets `AZURE_CLIENT_ID` so `DefaultAzureCredential` picks its user-assigned identity.

| Identity | Role | Scope | Why |
|---|---|---|---|
| `id-web-*` | AcrPull | registry | pull the web image (existing) |
| `id-web-*` | Cosmos DB Built-in Data Contributor | Cosmos account (existing) | read cards and state, write producer decisions through the Desk |
| `id-web-*` | Azure Service Bus Data Sender | topic `match-events` | replay button in the console; send only, no receive, no other entity |
| `id-worker-*` | AcrPull | registry | pull the worker image |
| `id-worker-*` | Azure Service Bus Data Receiver | subscription `match-events/worker` | read and settle its own subscription; also lets the KEDA scaler read the subscription properties (`topics/subscriptions/read` is in the role) |
| `id-worker-*` | Cosmos DB Built-in Data Contributor | database `watershed` only (`<account>/dbs/watershed`) | upsert events, cards and audit entries |
| `id-worker-*` | Foundry User (`53ca6127-db72-4b80-b1b0-d745d6d5456d`, formerly Azure AI User) | Foundry account `watershed-foundry-da` (other resource group) | call the model deployments through the project endpoint |
| `id-replay-*` | AcrPull | registry | pull the worker image |
| `id-replay-*` | Azure Service Bus Data Sender | topic `match-events` | publish the demo replay |
| developer (`principalId`) | Data Sender + Data Receiver, Cosmos Data Contributor | namespace, account (existing) | local runs |

Foundry role choice: Microsoft's Foundry RBAC page (learn.microsoft.com/azure/foundry/concepts/rbac-foundry, updated 2026-09-22) says to assign Foundry User on the Foundry resource for Entra calls, and not to use roles starting with "Cognitive Services" (so not Cognitive Services OpenAI User). Foundry User is also what your own account has today, and the Writer works with it locally. It is broader than needed (all `Microsoft.CognitiveServices/*` data actions, and `listkeys`, harmless here since local keys are disabled). A tighter built-in role exists, Foundry Project Runtime User (`142bfaed-a13f-4c2d-bed2-6db62c4a1009`, data action `accounts/AIServices/responses/*` only), but the page does not document it for this case: to try it, change the `foundryRoleId` default in `resources.bicep`, provision, and run one Writer call (the old assignment stays until removed). Foundry Agent Consumer is for published agent endpoints, not for an Agent Framework agent that calls a model deployment.

KEDA role: the KEDA page talks about Manage rights for connection strings and says nothing about Entra roles. Data Receiver on the subscription covers the read actions; if the scaler fails to authenticate (Container Apps system logs: `ContainerAppSystemLogs_CL`, look for the `match-events` rule), widen it to Data Receiver on the topic, then Data Owner on the topic as a last resort.

## What you must run, in order

From the main checkout (`code/`), signed in with `az login` and `azd auth login`:

1. Point azd at Foundry (one time):
   `azd env set FOUNDRY_ACCOUNT_ID /subscriptions/f8d79804-7583-4c52-996c-694b2376bca8/resourceGroups/rg-watershed-foundry/providers/Microsoft.CognitiveServices/accounts/watershed-foundry-da`
   `azd env set FOUNDRY_PROJECT_ENDPOINT <value from .env>` (the models and effort default to gpt-5.4-nano, gpt-5-mini, medium; set `FOUNDRY_MODEL` and so on only to change them). In Git Bash prefix the first command with `MSYS_NO_PATHCONV=1`, or the leading slash is rewritten.
2. Merge the code fix for the managed identity in `writer.py` (below), otherwise the Writer silently falls back to templates in Azure.
3. `azd provision`: identities, roles, worker app and job (placeholder image).
4. `azd deploy`: builds both images in ACR from the repository root and updates `ca-web-*` and `ca-worker-*`. It writes `SERVICE_WORKER_IMAGE_NAME` to the azd env.
5. `azd provision` again: the replay job picks up the worker image (azd does not deploy jobs). Later provisions keep the deployed images instead of the placeholder.
6. Stop the laptop worker, then run the demo: `az containerapp job start -n $(azd env get-value AZURE_REPLAY_JOB_NAME) -g $(azd env get-value AZURE_RESOURCE_GROUP)`. The worker starts within about 30 s of the first message. Logs: `az containerapp logs show -n $(azd env get-value AZURE_WORKER_APP_NAME) -g $(azd env get-value AZURE_RESOURCE_GROUP) --follow`.
7. Judging mode without a producer: `azd env set WORKER_EXTRA_ARGS --auto-approve` then `azd provision`.

New role assignments take 5 to 10 minutes to apply: a first run right after step 3 may fail with an authorization error.

## Easy Auth (off by default)

`WEB_AUTH_CLIENT_ID` empty: no auth config. Set: a Microsoft provider with `unauthenticatedClientAction: AllowAnonymous`, so the overlay stays public and a signed-in producer gets `X-MS-CLIENT-PRINCIPAL-NAME`. No client secret: without one, Easy Auth signs users in with the ID token only, which is all the console needs. The web app gets `EASY_AUTH_ENABLED` (`True` or `False`): the app must trust `X-MS-CLIENT-PRINCIPAL-NAME` only when it is `True`, because without Easy Auth any client can send that header. Producers sign in at `/.auth/login/aad?post_login_redirect_uri=/` (replace `/` with the producer route).

One-time app registration (not run):

```
WEB_URI=$(azd env get-value SERVICE_WEB_URI)
APP_ID=$(az ad app create --display-name watershed-web --sign-in-audience AzureADMyOrg --web-redirect-uris "$WEB_URI/.auth/login/aad/callback" --enable-id-token-issuance true --query appId -o tsv)
az ad app update --id $APP_ID --identifier-uris "api://$APP_ID"
az ad sp create --id $APP_ID
azd env set WEB_AUTH_CLIENT_ID $APP_ID
azd provision
```

Optional, to limit sign-in to named producers: `az ad sp update --id $APP_ID --set appRoleAssignmentRequired=true`, then add the users under Enterprise applications > watershed-web > Users and groups.

## Code changes proposed (src/worker is not mine)

**Managed identity for the Writer (blocking).** `FoundryWriter` builds `ManagedIdentityCredential()` with no client id. In azure-identity 1.26 that credential reads `AZURE_CLIENT_ID` only for workload identity, not on Container Apps, so with a user-assigned identity only it asks for a system identity that does not exist; `draft` catches the exception and uses the template (`writer.py`, "a model or network failure falls through to the template"), so the failure is easy to miss. In `src/worker/writer.py`:

```python
credential = ChainedTokenCredential(ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID")), AzureCliCredential())
```

Locally `AZURE_CLIENT_ID` is unset and the behaviour is unchanged.

**Why `listen` waits minutes after the last message.** Inference from the azure-servicebus 7.15 source, to confirm with a timed run. The iterator ends after `max_wait_time` (30 s) with no message: that part is expected. Then the loop asks for `NEXT_AVAILABLE_SESSION` again; with no session left, the accept times out after `max_wait_time`, and the receiver open goes through `_do_retryable_operation`, which treats `OperationTimeoutError` as retryable and tries `retry_total + 1 = 4` times with exponential back-off (0.8, 1.6, 3.2 s). That is about 30 + 4 x 30 + 6 = 156 s, more if the service holds the accept longer than the client timeout. Neither prefetch nor the `AutoLockRenewer` is involved: the renewer is registered only after a session is accepted. To confirm: run with `logging.getLogger("azure.servicebus").setLevel(logging.INFO)` and look for three "Retrying..." lines before "no session with messages".

Fix in `src/worker/azure_io.py`: no client retries, and a failed settlement no longer kills the loop (the message comes back after its lock expires and the pipeline drops it as a duplicate, since dedup is by message id):

```python
from azure.servicebus.exceptions import OperationTimeoutError, ServiceBusError

    # retry_total=0: each retry of a NEXT_AVAILABLE_SESSION accept waits max_wait_time again, so the default (3) quadruples the idle stop
    with ServiceBusClient(fqdn, credential=DefaultAzureCredential(), retry_total=0) as client, AutoLockRenewer(max_lock_renewal_duration=4 * 3600) as renewer:
        ...
                            receiver.complete_message(msg)
                        except (SchemaError, json.JSONDecodeError, KeyError) as error:
                            receiver.dead_letter_message(msg, reason="unreadable", error_description=str(error)[:4000])
                        except ServiceBusError as error:  # settlement failed: redelivered after the lock expires, then dropped as a duplicate
                            log.warning("could not settle %s: %s", msg.message_id, error)
```

Expected stop: about 2 x `idle_s` (60 s). With the KEDA design the delay only costs a few idle minutes, so this fix matters for laptop runs more than for Azure.

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Worker restarts mid-match (crash, new revision, platform move) | Match state is in memory; completed messages are gone, so the restarted worker sees the match from that point (score, win probability wrong) | `maxReplicas 1`, single revision; do not deploy during a replay; restart the replay if it happens. A durable fix (rebuild state from the Cosmos `events` container) is code work |
| `listen` exits during a pause longer than 30 s mid-match | The loop restarts it in 5 s and the same process keeps its state, so no loss | Already handled by the loop; the process is not restarted |
| Writer credential (above) not fixed | Template cards instead of Foundry cards, without an obvious error | Merge the one-line change before deploying |
| KEDA scaler auth with Data Receiver at subscription scope not documented | Worker never starts | Check system logs; widen as described above |
| Foundry User is broader than strictly needed | Worker identity could use any data action on the Foundry account | Try Foundry Project Runtime User later |
| azd parameter defaults (`${VAR=}`) | If azd prompts or fails on empty values, set them explicitly with `azd env set` | Preview ran without prompting |
| Placeholder image in the replay job until step 5 | Starting the job before the second provision fails (no python in the placeholder) | Follow the order above |
| Easy Auth off and header trusted | Anyone can impersonate a producer by sending `X-MS-CLIENT-PRINCIPAL-NAME` | Web code must check `EASY_AUTH_ENABLED` |
| `DESK=cosmos` naming | Web agent may expect another variable name | Align with the web chantier; `COSMOS_ENDPOINT` is also set |
| Existing non-Entra pieces | The environment still reads the Log Analytics shared key (`listKeys`, platform log shipping) and the web app has the App Insights connection string (an ingestion id, not a credential) | Acceptable for the hackathon; full keyless would mean disabling local auth on App Insights and granting Monitoring Metrics Publisher |
| Image size | `agents` extra pulls the whole Agent Framework; first ACR build takes several minutes | One-time |
