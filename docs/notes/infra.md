# Infra: worker and replay in Azure, web app settings, Easy Auth, reset

CI also builds both images and imports what each runs (`images` job in `.github/workflows/ci.yml`: `web.app` in the web image; the Writer, `agent_framework.foundry` and `azure.servicebus` in the worker image). Checked on 25/10/2026, locally and without any Azure call: `az bicep build` passes with no warning on `infra/main.bicep` and `infra/killswitch/main.bicep` (Bicep 0.48.1); the worker command of the image, run on the demo replay with Service Bus, Cosmos DB and Foundry replaced by fakes, listens with `--forever`, loads the history and writes the storyline, the insights and the cards; `python -m league history --seed 9 --seasons 50` takes about 6 s and writes 2.5 MB. Deployed on 26/10/2026 in the order below, images built remotely in ACR by `azd deploy` (no local Docker daemon); the reference replay of that day ran on it (`docs/evaluation.md`, "Live run, production prompt").

## Files

| File | What it holds |
|---|---|
| `azure.yaml` | Services `web` and `worker`, both built in ACR from the repository root (`docker.context: ../..`, relative to the project folder); hooks `preprovision` (fails while `FOUNDRY_ACCOUNT_ID` or `FOUNDRY_PROJECT_ENDPOINT` is unset) and `postdeploy` (`az containerapp job update --container-name replay --image <SERVICE_WORKER_IMAGE_NAME>` on the replay job), each in `sh` and in `pwsh` |
| `.dockerignore` | Repository root: excludes `.git`, `.claude`, `.azure`, `.env*`, `.venv`, `docs`, `spikes`, `tests`, `infra`, `prep-*`, and `data/*` except `data/replay` |
| `src/worker/Dockerfile` | python:3.12-slim, `pip install ".[azure,agents]"`, `data/replay`, the league history generated at build in `data/history/9`, a build step that loads it as the runtime user (uid 10001), and `python -m worker --live --cosmos --writer foundry --forever --history data/history/9 --league-seed 9 --matchday 30 $WORKER_EXTRA_ARGS` in a restart loop |
| `infra/resources.bicep` | One identity per app; worker Container App with a KEDA Service Bus rule (cool-down 900 s); `replay` Container Apps Job; web app settings (below); `APPLICATIONINSIGHTS_CONNECTION_STRING` for the web app and the worker (traces, below); optional Easy Auth; Foundry role through a module. Container Apps API 2025-01-01, needed for the scale rule identity and `cooldownPeriod` |
| `infra/modules/foundry-role.bicep` | Role assignment on the Foundry account, deployed in its resource group (read from `FOUNDRY_ACCOUNT_ID`) |
| `infra/main.bicep`, `main.parameters.json` | Parameters from azd variables: `SERVICE_WEB_IMAGE_NAME`, `SERVICE_WORKER_IMAGE_NAME`, `FOUNDRY_ACCOUNT_ID`, `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL` (gpt-5.4-nano), `FOUNDRY_REASONING_EFFORT` (medium), `FOUNDRY_MAX_TOKENS` (5000), `FOUNDRY_RECAP_REASONING_EFFORT` (low), `FOUNDRY_RECAP_MAX_TOKENS` (6000), `WORKER_EXTRA_ARGS`, `WORKER_MIN_REPLICAS` (0), `WEB_AUTH_CLIENT_ID`, `PRODUCERS`, `WEB_MIN_REPLICAS` (0), `RATE_LIMIT_PER_MIN` (600); outputs `AZURE_RESOURCE_GROUP`, `AZURE_CONTAINER_REGISTRY_ENDPOINT`, `AZURE_CONTAINER_REGISTRY_NAME`, `SERVICE_WEB_URI`, `SERVICE_BUS_FQDN`, `SERVICE_BUS_TOPIC`, `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, `AZURE_WORKER_APP_NAME`, `AZURE_REPLAY_JOB_NAME` |
| `src/generator/publish.py` | The replay publisher the job runs; `--min-var-delay` (below) |
| `src/worker/reset.py` | Deletes every document of one match before a new replay (below) |

## Worker: one process per replica, scaled by KEDA

The worker runs as a Container App with no ingress, `minReplicas` from `WORKER_MIN_REPLICAS` (0 by default), `maxReplicas 1` and an `azure-servicebus` scale rule on the `worker` subscription (`messageCount 1`), authenticated with the worker identity. When a replay starts, KEDA sees a message waiting (it polls every 30 s) and starts one replica. Cost when idle: zero, unless `WORKER_MIN_REPLICAS` is 1.

Inside the replica, `--forever` keeps one `python -m worker` process waiting for sessions. The match state lives in its memory, and the demo replay has seven quiet stretches of more than 30 s at speed 10, after which a listener without `--forever` stops. A new process does not get the state back: it sees the rest of the match without its beginning (no match information, so no new card). The shell loop only restarts the process 5 s after a crash, instead of the growing back-off Container Apps applies to a crashing container.

KEDA stops the replica after its cool-down, counted from the last poll that saw a message waiting. The worker settles each message within seconds, so a poll can find the subscription empty while a replay runs, more often in the stretches played in real time; with the default 300 s, ten such polls in a row stop the replica mid-match. The cool-down is 900 s (`cooldownPeriod`): thirty polls in a row must then find nothing, less likely but not impossible (inference). It costs up to 15 more minutes of one replica (0.5 vCPU, 1 GiB) after each replay. `WORKER_MIN_REPLICAS=1` removes the risk while it is set: one replica stays up, and is billed, between replays.

`maxReplicas 1` keeps a single consumer, since the state is in memory. `activeRevisionsMode: Single` is what Microsoft asks for with non-HTTP scale rules. Finished matches stay in memory until the replica stops (about 10 MB each, `docs/notes/worker-io.md`), and a replay of a match the replica has already seen is dropped as duplicates: see "Reset between two replays".

The league history (seed 9, 50 seasons) is generated during the image build rather than copied: it is deterministic for a seed and comes from the code in `src/`, so it always matches the names and the league of the image, including after a change of name generator. `--league-seed 9 --matchday 30` is the season of the demo match (`DEMO_MATCHDAY` in `src/league/season.py`, also the default of `--matchday`).

`WORKER_EXTRA_ARGS` is appended last to the command, for `--auto-approve` in judging mode. The template sets it as a container variable from the azd variable of the same name; the container variable was `WORKER_ARGS` before, so provision and deploy together.

## Traces (Application Insights)

The template gives the worker `APPLICATIONINSIGHTS_CONNECTION_STRING` (the web app already had it). With the variable set, `src/worker/telemetry.py` configures the export when the worker starts: the OpenTelemetry SDK with `AzureMonitorTraceExporter` from azure-monitor-opentelemetry-exporter (`opentelemetry-sdk` and the exporter are in the `agents` extra, so in the image; the tests run on the same versions). The worker exports traces only, not its logs or metrics. If azure-monitor-opentelemetry happens to be installed, its `configure_azure_monitor` is used instead, with its default instrumentations; the image does not install it and it has not been run here. It then turns on the Agent Framework instrumentation with `enable_instrumentation(enable_sensitive_data=False)`, which overrides `ENABLE_SENSITIVE_DATA`. Without the variable (local runs, tests) nothing is configured and the spans below cost a function call. A bad connection string or a missing package is logged, and the worker runs without traces.

| Exported | Not exported |
|---|---|
| One span per draft of the desk: `desk.write` (card), `desk.write_insight` (insight or storyline), `desk.write_recap`; attributes `match_id`, `card_id`, `kind`, `writer`, `verified`, `attempts`, `fallback_reason`, `status`, `version`, `deployment`, `prompt_sha`, and when the document has a `verification.attempts_log` the token totals of its attempts (`input_tokens`, `output_tokens`, `reasoning_tokens`) and the last `finish_reason` | The texts of the cards, the duo, the insights, the storyline and the recap: no span attribute carries a text |
| One span per card action from the feed: `desk.action` (hold, release, retract, rewrite), with `action`, `cause`, the resulting `status` and `version`, and for a rewrite the fields of the new draft | The prompts, the system instructions and the model replies: the Agent Framework only records them with sensitive data on, which the worker forces off |
| The Agent Framework spans of each model call, children of the desk span: model, duration, token counts, finish reason, error type | Exception messages and stacks, on the desk spans and on the Agent Framework spans: a desk span that an exception crosses (a recap writer that fails, for instance) keeps only `error.type` and an error status |
| | The worker's Python logs, which can quote an error message (`str(error)`, cut to 200 or 300 characters): they stay in the container logs that the Container Apps environment ships to Log Analytics, not in Application Insights |
| | Producer decisions (approve, edit, reject, feedback): the web app makes them, the worker does not see them. The desk spans cover the actions the feed causes (hold, release, retract, rewrite) and the status the auto-approval sets |

Cost: the Application Insights resource is workspace-based, so what it ingests counts against the Log Analytics daily cap of 0.2 GB (`workspaceCapping` in `infra/resources.bicep`; 5 GB a month are free). The desk and model spans are a few hundred per match at most (40 model calls per match, a few dozen drafts), well under 1 MB (estimate, not measured). The SDK path does not trace the Azure SDK calls; with `configure_azure_monitor` they would be the bulk (the demo has 1 761 messages, each stored in Cosmos DB, so a few thousand dependency records per match, a few MB; estimate). Beyond the cap, ingestion stops until the next day for the whole workspace, Container Apps logs included: a runaway loop cannot turn into a bill, but it can blind the logs for the rest of the day.

To read them: Application Insights > Transaction search or Logs (`dependencies` table, `cloud_RoleName` `watershed-worker`), filter on `customDimensions.card_id`.

The spans are sent in batches (`BatchSpanProcessor`, every 5 s by default) and flushed at a normal exit. A container stopped by SIGTERM, at a KEDA scale-in or a new revision, does not run the exit handlers: the spans of its last few seconds are lost. The desk's state is in Cosmos DB, so only those traces are missing.

## Identities and roles

All assignments are Azure RBAC or Cosmos DB data-plane RBAC, no key and no connection string. Every app gets `AZURE_CLIENT_ID` so `DefaultAzureCredential` picks its user-assigned identity.

| Identity | Role | Scope | Why |
|---|---|---|---|
| `id-web-*` | AcrPull | registry | pull the web image |
| `id-web-*` | Cosmos DB Built-in Data Contributor | Cosmos account | read the desk and the feed, write producer decisions and feedback through the Desk |
| `id-worker-*` | AcrPull | registry | pull the worker image |
| `id-worker-*` | Azure Service Bus Data Receiver | subscription `match-events/worker` | read and settle its own subscription; also lets the KEDA scaler read the subscription properties (`topics/subscriptions/read` is in the role) |
| `id-worker-*` | Cosmos DB Built-in Data Contributor | database `watershed` only (`<account>/dbs/watershed`) | upsert events, cards, insights, match state, recap and audit entries |
| `id-worker-*` | Foundry User (`53ca6127-db72-4b80-b1b0-d745d6d5456d`, formerly Azure AI User) | Foundry account (other resource group) | call the model deployments through the project endpoint |
| `id-replay-*` | AcrPull | registry | pull the worker image |
| `id-replay-*` | Azure Service Bus Data Sender | topic `match-events` | publish the demo replay |
| developer (`principalId`) | Data Sender + Data Receiver, Cosmos Data Contributor | namespace, account | local runs, the replay from a laptop, `worker.reset` |

The web app has no Service Bus role and no Service Bus setting: the console has no replay button and reads the feed from Cosmos DB. Removing a role assignment from the template does not remove it from Azure (deployments are incremental): if the web identity ever got Azure Service Bus Data Sender on the topic, list and delete it by hand.

```
az role assignment list --assignee <principal id of id-web-<suffix>> --all -o table
az role assignment delete --assignee <principal id of id-web-<suffix>> --role "Azure Service Bus Data Sender" --scope <resource id of the match-events topic>
```

Foundry role choice: Microsoft's Foundry RBAC page (learn.microsoft.com/azure/foundry/concepts/rbac-foundry, updated 2026-09-22) says to assign Foundry User on the Foundry resource for Entra calls, and not to use roles starting with "Cognitive Services" (so not Cognitive Services OpenAI User). Foundry User is also the developer account's role, with which the Writer works locally. It is broader than needed (all `Microsoft.CognitiveServices/*` data actions, and `listkeys`, harmless here since local keys are disabled). A tighter built-in role exists, Foundry Project Runtime User (`142bfaed-a13f-4c2d-bed2-6db62c4a1009`, data action `accounts/AIServices/responses/*` only), but the page does not document it for this case: to try it, change the `foundryRoleId` default in `resources.bicep`, provision, and run one Writer call (the old assignment stays until removed). Foundry Agent Consumer is for published agent endpoints, not for an Agent Framework agent that calls a model deployment.

KEDA role: the KEDA page talks about Manage rights for connection strings and says nothing about Entra roles. Data Receiver on the subscription covers the read actions; if the scaler fails to authenticate (Container Apps system logs: `ContainerAppSystemLogs_CL`, look for the `match-events` rule), widen it to Data Receiver on the topic, then Data Owner on the topic as a last resort.

## Web app settings

| Setting | Source | Default | Effect |
|---|---|---|---|
| `PUBLIC_MODE` | template | `1` | Every visitor sees the console read-only (desk, curve, graphics, insights and storyline); approve, edit, reject, thumbs, reasons and notes need a producer signed in through Easy Auth, and the web app refuses them server-side otherwise (`src/web/security.py`). Locally, `PUBLIC_MODE=0` keeps the local producer |
| `EASY_AUTH_ENABLED` | template, from `WEB_AUTH_CLIENT_ID` | `False` | The app trusts `X-MS-CLIENT-PRINCIPAL-NAME` only when it is `True`: without Easy Auth any client can send that header |
| `PRODUCERS` | azd `PRODUCERS` | empty | Optional comma-separated allow-list of `X-MS-CLIENT-PRINCIPAL-NAME` values; empty: any user Easy Auth signs in |
| `RATE_LIMIT_PER_MIN` | azd `RATE_LIMIT_PER_MIN` | 600 | Requests per minute and per client IP on the pages and the Dash callbacks, 429 beyond. Counted in each gunicorn process (`src/web/security.py`): with 2 processes per replica and 2 replicas, one client can get up to four times the limit |
| `minReplicas` | azd `WEB_MIN_REPLICAS` | 0 | 0 scales to zero when nobody visits, with a cold start on the next visit; 1 keeps one replica warm (billed at the idle rate when it serves nothing), for example while the demo is judged |
| `DESK`, `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | template | `cosmos` | The console reads and writes the desk in Cosmos DB |

## Deployment, in order

From the repository root, signed in with `az login` and `azd auth login`:

1. Point azd at Foundry (one time):
   `azd env set FOUNDRY_ACCOUNT_ID /subscriptions/<subscription-id>/resourceGroups/<foundry-resource-group>/providers/Microsoft.CognitiveServices/accounts/<foundry-account>`
   `azd env set FOUNDRY_PROJECT_ENDPOINT <value from .env>` (the model, the efforts and the output budgets default to gpt-5.4-nano, medium and 5,000 tokens for the cards, low and 6,000 for the recap; set `FOUNDRY_MODEL` and so on only to change them). In Git Bash prefix the first command with `MSYS_NO_PATHCONV=1`, or the leading slash is rewritten.
2. `azd up`, or `azd provision` then `azd deploy`. The `preprovision` hook stops at once if one of the two Foundry variables is missing. Provision creates the identities, the roles, the apps and the job (placeholder image the first time); deploy builds both images in ACR from the repository root (the worker build also generates the history), updates `ca-web-*` and `ca-worker-*` and writes `SERVICE_WORKER_IMAGE_NAME` to the azd environment; the `postdeploy` hook then sets that image on the replay job (azd does not deploy jobs), which a second provision used to do. Later provisions keep the deployed images instead of the placeholder. On Windows azd runs the `pwsh` version of the hooks: PowerShell 7 must be installed (`winget install Microsoft.PowerShell`) if azd does not fall back to Windows PowerShell; the scripts also run in 5.1.
3. Stop the laptop worker, then run the demo: `az containerapp job start -n $(azd env get-value AZURE_REPLAY_JOB_NAME) -g $(azd env get-value AZURE_RESOURCE_GROUP)`. The worker starts within about 30 s of the first message. Logs: `az containerapp logs show -n $(azd env get-value AZURE_WORKER_APP_NAME) -g $(azd env get-value AZURE_RESOURCE_GROUP) --follow`.

New role assignments take 5 to 10 minutes to apply: a first run right after the first provision may fail with an authorization error. A change of an azd variable reaches the apps at the next `azd provision`.

## Judging and the video

| | Judging | Video with a producer |
|---|---|---|
| Cards on air | `azd env set "WORKER_EXTRA_ARGS=--auto-approve"`, then `azd provision` | Approved by the producer, signed in through Easy Auth (`WEB_AUTH_CLIENT_ID` set, below); `WORKER_EXTRA_ARGS` empty: `azd env set "WORKER_EXTRA_ARGS="` (Windows PowerShell 5.1 drops an empty `""` argument) or remove the line from `.azure/<env>/.env`, then `azd provision` |
| Replicas | `azd env set WEB_MIN_REPLICAS 1` and `azd env set WORKER_MIN_REPLICAS 1` for the judging period, back to 0 after | 0 |
| Replay | The job (`--speed 10`) | From the laptop, with time to approve a goal card before its VAR check: `python -m generator.publish data/replay/demo-0007.jsonl.gz --speed 10 --min-var-delay 45` (it reads `SERVICE_BUS_FQDN` from the azd environment; the developer account has Data Sender on the namespace) |

At speed 10 the demo's three VAR checks start 15 to 17 s after their goal, while the Writer and a producer need longer. `--min-var-delay 45` holds each check until 45 real seconds after its goal; the check itself keeps its length, and the replay takes 1,542 s instead of 1,454 s.

## Reset between two replays

A replay reuses the same ids. A second replay of a match would mix with what the first one left (producer decisions, audit trail, feedback, recap), and a worker that has already seen the match drops its messages as duplicates. Before replaying:

1. `python -m worker.reset --match demo-0007 --cosmos` lists the match's documents in `events`, `cards` (cards, match state, insights, storyline, recap, feedback) and `audit`; add `--yes` to delete them. Locally, `--desk-dir data/desk` instead of `--cosmos` deletes `data/desk/demo-0007/`. It signs in with `az login` (Entra ID, never a key) and uses the developer's Cosmos DB data role from `azd provision`. The demo leaves about 1,900 documents, 1,761 of them events, deleted one by one, with throttling retried.
2. Restart the worker: restart its revision, or, with `WORKER_MIN_REPLICAS` at 0, wait until KEDA has stopped the replica (15 minutes after the last message):
   ```
   APP=$(azd env get-value AZURE_WORKER_APP_NAME); RG=$(azd env get-value AZURE_RESOURCE_GROUP)
   az containerapp revision restart -n $APP -g $RG --revision $(az containerapp show -n $APP -g $RG --query properties.latestRevisionName -o tsv)
   ```
   "Restart succeeded" comes back before the old replica has stopped: wait until `az containerapp replica list -n $APP -g $RG` shows no replica, or only one created after the restart, before step 3. On 26/10 a replay started 14 s after the restart: the old replica read the match info and the kick-off, then stopped; the new one took the session without them, ignored every action, and wrote no card.
3. Start the replay. The web app caches the feed it has read; the ids are the same on a replay, so its graphics stay consistent, and restarting its revision clears the cache if needed.

## Easy Auth

The deployed web app has it, so that a producer can sign in. `WEB_AUTH_CLIENT_ID` empty: no auth config, and in public mode nobody can approve online. Set: a Microsoft provider with `unauthenticatedClientAction: AllowAnonymous`, so visitors keep the read-only console and the overlay, and a signed-in producer gets `X-MS-CLIENT-PRINCIPAL-NAME`. No client secret: without one, Easy Auth signs users in with the ID token only, which is all the console needs. The template sets `EASY_AUTH_ENABLED` to `True` only when the auth config exists. Producers sign in at `/.auth/login/aad?post_login_redirect_uri=/` (replace `/` with the producer route).

One-time app registration, in bash (Git Bash on Windows):

```
WEB_URI=$(azd env get-value SERVICE_WEB_URI)
APP_ID=$(az ad app create --display-name watershed-web --sign-in-audience AzureADMyOrg --web-redirect-uris "$WEB_URI/.auth/login/aad/callback" --enable-id-token-issuance true --query appId -o tsv)
az ad app update --id $APP_ID --identifier-uris "api://$APP_ID"
az ad sp create --id $APP_ID
azd env set WEB_AUTH_CLIENT_ID $APP_ID
azd provision
```

To limit sign-in to named producers: `az ad sp update --id $APP_ID --set appRoleAssignmentRequired=true`, then add the users under Enterprise applications > watershed-web > Users and groups; or, inside the app, `azd env set PRODUCERS <name>` with the sign-in names.

## Code this relies on

- The Writer and the recap writer try `ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)` first, then the Azure CLI (`src/worker/writer.py`, `src/worker/recap.py`). With a user-assigned identity only, a managed identity credential without the client id asks for a system identity that does not exist, and the Writer falls back to templates without an obvious error.
- `listen` opens sessions with `retry_total=0` (`src/worker/azure_io.py`): the SDK's own retries of a "no session" timeout kept an idle worker alive for about 2.5 minutes (`docs/notes/worker-io.md`).

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Worker restarts mid-match (crash, new revision, platform move, scale-in) | A restarted worker does not keep its state: it sees the rest of the match without its beginning (no match information, so no new card) | `--forever`, `maxReplicas 1`, single revision, 15-minute cool-down, `WORKER_MIN_REPLICAS=1` while the demo is judged; no deploy or provision during a replay; if it happens, reset and replay. A durable fix (rebuild the state from the Cosmos `events` container) is code work |
| Quiet stretch of more than 30 s mid-match (seven in the demo at speed 10) | Without `--forever` the listener stops and the next process starts without the match | `--forever`: the same process keeps waiting, with its state |
| Second replay of a match the worker has seen | Dropped as duplicates: the desk does not change | Reset and restart the worker (above) |
| Image and template out of step | An image that reads `WORKER_EXTRA_ARGS` on an app still provisioned with `WORKER_ARGS` loses `--auto-approve` | Provision and deploy together |
| Easy Auth not configured (a new environment) | In public mode nobody is a producer online | Register the app (above); judging runs in auto-approve |
| KEDA scaler auth with Data Receiver at subscription scope not documented | Worker never starts | Check system logs; widen as described above |
| Foundry User is broader than strictly needed | Worker identity could use any data action on the Foundry account | Try Foundry Project Runtime User later |
| azd parameter defaults (`${VAR=}`) | If azd prompts or fails on empty values, set them explicitly with `azd env set` | The last preview ran without prompting |
| `WORKER_MIN_REPLICAS`, `WEB_MIN_REPLICAS` and `RATE_LIMIT_PER_MIN` are int parameters fed with azd strings | azd turns numeric strings into numbers for int parameters; if a provision fails on their type, declare them as strings in `main.bicep` and convert with `int()` | The provisions of 26/10 passed with them |
| Placeholder image in the replay job until the `postdeploy` hook has run | Starting the job after a provision alone, before any deploy, fails (no python in the placeholder); a failed hook (no `az` on the PATH, not signed in) leaves it so | Run `azd deploy`, or the hook's command by hand: `az containerapp job update -n <job> -g <rg> --container-name replay --image $(azd env get-value SERVICE_WORKER_IMAGE_NAME)` |
| Existing non-Entra pieces | The environment still reads the Log Analytics shared key (`listKeys`, platform log shipping) and the web app and the worker have the App Insights connection string (an ingestion id, not a credential) | Acceptable for the hackathon; full keyless would mean disabling local auth on App Insights and granting Monitoring Metrics Publisher |
| The trace export packages are not pinned in `src/worker/constraints.txt` | The image installs the latest `opentelemetry-sdk` and `azure-monitor-opentelemetry-exporter` that accept the pinned `opentelemetry-api==1.45.1` (the exporter 1.0.0b58 asks `opentelemetry-api~=1.45.0` and `opentelemetry-sdk~=1.45.0`), and the latest `msrest`, `oauthlib`, `requests-oauthlib` and `psutil`: two builds can differ | Add to `src/worker/constraints.txt` the 7 versions of the development environment where the suite passes: `azure-monitor-opentelemetry-exporter==1.0.0b58`, `msrest==0.7.1`, `oauthlib==4.0.0`, `opentelemetry-sdk==1.45.1`, `opentelemetry-semantic-conventions==0.66b1`, `psutil==7.2.2`, `requests-oauthlib==2.0.0` |
| Image size and build time | The `agents` extra pulls the whole Agent Framework; the history adds about 10 s and 2.5 MB | Once per build |
