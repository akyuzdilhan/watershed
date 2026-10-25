# Security note: public repository and public demo URL

State read on 24/10/2026 with read-only `az` and `gh` calls. The repository `akyuzdilhan/watershed` is already public; GitHub secret scanning and push protection are already enabled on it; Dependabot alerts, Dependabot security updates, private vulnerability reporting and branch protection are not. Updated on 25/10/2026, without any Azure call, for the read-only public console, the web app settings and the kill switch that leaves the web app running.

## Threats and what covers them

| # | Threat | Covered by | Status |
|---|---|---|---|
| 1 | A secret is committed (key, connection string, token) | No key exists: Service Bus, Cosmos DB and Foundry have local auth disabled (`disableLocalAuth: true`, checked on the Foundry account); apps use managed identities. `.env` is ignored. gitleaks in CI on the whole history; GitHub secret scanning and push protection | Done. gitleaks 8.30.1 run locally on the working tree and the 10 commits: no leak |
| 2 | A visitor runs up the model bill through the public URL | The web app makes no model call and has no Foundry role: only the worker calls the models. Per-deployment rate limits (20k tokens and 20 requests per minute each, GlobalStandard capacity 20); `max_calls_per_match=40` in `src/worker/desk.py` (beyond it, cards use the template); a visitor can neither start a replay nor write anything; budget kill switch | Done |
| 3 | Compute or platform costs grow without a model call | Web app: 0.25 vCPU and 0.5 GiB, `maxReplicas: 2`, `minReplicas` from `WEB_MIN_REPLICAS` (0 by default), per-IP limit `RATE_LIMIT_PER_MIN` (600 by default); worker: 0.5 vCPU and 1 GiB, `maxReplicas: 1`, `minReplicas` from `WORKER_MIN_REPLICAS` (0 by default), KEDA cool-down 15 minutes; Log Analytics daily cap 0.2 GB; Cosmos DB free tier with 1000 RU/s shared and no autoscale; ACR Basic | Done. The kill switch does not stop the web app (see "What remains") |
| 4 | The cost alert arrives too late | Budget `hackathon-budget` (50 CAD, subscription) and `foundry-budget` (10 CAD, Foundry resource group) send e-mails; the kill switch stops the worker, the replay and the models at 80 % and again at 90 % actual | To deploy (below). Cost data lags (see "What remains") |
| 5 | Anyone uses the producer actions (approve, edit, reject, thumbs, reasons, notes) on the public demo | Public mode (`PUBLIC_MODE=1`, set by the template): every visitor gets the console read-only, and the web app refuses each producer action server-side unless Easy Auth has signed in a producer (`src/web/security.py`); `EASY_AUTH_ENABLED` says whether the sign-in header can be trusted; `PRODUCERS` can name the producers | Code and template done; Easy Auth needs its app registration (`docs/notes/infra.md`) |
| 6 | Prompt injection into the Writer | Inputs are synthetic and produced by the code, and visitors cannot send text. The Writer gets facts through `get_moment`; the deterministic Verifier rejects any number that is not claimed and matched, then falls back to a template. Producer notes reach the Writer prompt only through the feedback profile, which the worker reads only when `FEEDBACK_PROFILE` is set (the templates do not set it), and only a signed-in producer can write a note | Done for the current design. Any free text from a visitor sent to the model would reopen it |
| 7 | A vulnerable or hijacked dependency or action | Dependabot version updates (pip at the root and in `src/web`, the `src/web` Docker image, GitHub Actions, weekly); Dependabot alerts (to enable); actions pinned to major tags. The base image of `src/worker/Dockerfile` is not watched | Config done; alerts to enable; a Docker entry for `src/worker` to add |
| 8 | A workflow token or fork pull request is abused | `permissions: contents: read` at the top, `pull-requests: read` on the gitleaks job only, `persist-credentials: false`, no `pull_request_target`, no Azure login, no secret used | Done |
| 9 | Someone pushes or force-pushes to `main` | Branch protection (commands below) | To enable |
| 10 | The kill switch webhook URI leaks | It is a secret: passed as a `@secure()` parameter, never printed, not stored in deployment history. A leak can only stop the pipeline and the models, not read data or spend money | Accepted |
| 11 | Personal data in the repository | Synthetic data only: every match, player and event comes from the seeded generator | Done |

Facts on the Foundry deployments come from `az cognitiveservices account deployment list`; the call cap from `src/worker/desk.py`; the other caps from `infra/resources.bicep`.

Worst case on the models, inference: two deployments at 20k tokens per minute can process at most about 58 million tokens a day. At output prices of the order of one to two US dollars per million tokens, that is tens of dollars a day, more than the whole budget in a day or two. The rate limits bound the damage per hour; the call cap and the read-only console keep it near zero in normal use; the kill switch is the last net.

## GitHub settings to run

All are reversible. Run once, logged in with `gh auth login` as the repository owner.

```
# Secret scanning and push protection (already enabled; idempotent)
gh api -X PATCH repos/akyuzdilhan/watershed --input - <<'EOF'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}
EOF

# Dependabot alerts, then Dependabot security updates (pull requests for vulnerable dependencies)
gh api -X PUT repos/akyuzdilhan/watershed/vulnerability-alerts
gh api -X PUT repos/akyuzdilhan/watershed/automated-security-fixes

# Private vulnerability reporting (the channel SECURITY.md points to)
gh api -X PUT repos/akyuzdilhan/watershed/private-vulnerability-reporting

# Branch protection on main: CI must pass, no force push, no deletion.
# The check names are the CI job names; they must have run once on GitHub before they show up as passed.
gh api -X PUT repos/akyuzdilhan/watershed/branches/main/protection --input - <<'EOF'
{
  "required_status_checks": {"strict": false, "checks": [{"context": "test"}, {"context": "bicep"}, {"context": "secrets"}]},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

These commands use bash heredocs (Git Bash on Windows). With `enforce_admins: false`, the owner can still push to `main` directly when needed; the status checks then block pull requests and the force-push and deletion rules still hold. Set it to `true` to require CI on every push, at the cost of working through pull requests.

Check afterwards: `gh api repos/akyuzdilhan/watershed --jq .security_and_analysis` and `gh api repos/akyuzdilhan/watershed/branches/main/protection`.

## Kill switch

### What it does

Budget notification (actual > 80 %, and > 90 %) on `hackathon-budget` -> action group `ag-killswitch-*` -> Automation runbook receiver (webhook) -> runbook `Stop-Watershed` with `Mode=Kill`. Files are in `infra/killswitch/`; `infra/main.bicep` does not reference them, so `azd up` and `azd down` never touch the kill switch, which lives in its own resource group `rg-watershed-killswitch`.

It stops what can spend and leaves the web app running, so the public console stays up, read-only, with what was already produced. The web app makes no model call: the model bill stops with the worker and the deployments.

| Target | Action | Why this one (Microsoft docs) |
|---|---|---|
| The worker container app in `rg-watershed-dev` (tag `azd-service-name=worker`) | `POST .../containerApps/{name}/stop` | Container Apps has Start and Stop operations (REST API, available in 2024-03-01). Stopping deactivates the app: no replica and no compute billed, and KEDA starts none however many messages wait. `start` brings back the same revision, so the restore needs no saved configuration. Setting `maxReplicas` to 0 is not allowed |
| Running executions of the Container Apps jobs in `rg-watershed-dev` (the replay job) | `POST .../jobs/{job}/executions/{execution}/stop` | A replay in progress stops publishing. A manual job runs nothing else, and the identity cannot start one |
| The `worker` subscription of each topic in `rg-watershed-dev` | `PUT` with `status: Disabled` | `EntityStatus` supports `Disabled` on subscriptions: nothing is delivered and nothing can be received. The runbook reads the subscription and sends its properties back, because a PUT replaces the resource and `requiresSession` cannot change after creation |
| Foundry deployments of `watershed-foundry-da` | Print settings (`RESTORE-SETTINGS` lines in the job output), then `DELETE` | A GlobalStandard deployment has a minimum capacity of 1, so it cannot be scaled to 0. Deleting is the only way to make every call fail. The models' settings are in `restore.ps1` and in the job output |
| Every other container app (the web app) and subscription | None: listed as `KEEP` | The public console stays visible |

The runbook is Windows PowerShell 5.1, gets its token from the Automation managed identity endpoint (`IDENTITY_ENDPOINT`, `IDENTITY_HEADER`) and only calls the ARM REST API, so it needs no Az module. It discovers resources by listing them, so it does not depend on the `<suffix>` of the resource names, and it recognises the worker by its azd tag. Each part is independent: if a listing or an action fails, the other parts still run and the job ends as Failed with the list of failures. Run by hand, `Mode` defaults to `DryRun`, which only prints what it would do; only the webhook sets `Mode=Kill`. `tests/test_killswitch.py` runs the runbook and `restore.ps1` against fakes of the ARM API and of `az`: dry run, kill, a listing refused to the runbook, a failed listing in the restore.

### Roles of the Automation identity

A custom role, `Watershed kill switch operator`, assignable only on `rg-watershed-dev` and `rg-watershed-foundry`, assigned on these two resource groups and nowhere else:

| Action | Used for |
|---|---|
| `Microsoft.App/containerApps/read`, `Microsoft.App/containerApps/stop/action` | List the container apps and stop the worker |
| `Microsoft.App/jobs/read`, `Microsoft.App/jobs/executions/*` | List the jobs, list and stop their executions |
| `Microsoft.ServiceBus/namespaces/read`, `.../topics/read`, `.../topics/subscriptions/read`, `.../topics/subscriptions/write` | List and disable the worker subscription |
| `Microsoft.CognitiveServices/accounts/deployments/read`, `.../deployments/delete` | Record and delete the deployments |

No `start` (neither `containerApps/start/action` nor `jobs/start/action`) and no deployment `write`: the identity can stop, not restart or create, so a misuse of it cannot add cost. At resource group scope the role could also stop the web app; the runbook does not. `jobs/executions/*` is a wildcard because the exact name of the stop action was not checked against Microsoft's operation list (inference: `Microsoft.App/jobs/executions/stop/action`); everything under it reads or stops executions. Built-in roles would be broader (Contributor on each resource group, or Container Apps Contributor plus Azure Service Bus Data Owner plus Cognitive Services Contributor). No data-plane role: the identity cannot read Cosmos DB or Service Bus messages. Automation local auth stays enabled because webhooks do not work without it.

### Deploy

Prerequisite: Owner (or User Access Administrator plus Contributor) on the subscription, to create the custom role and its assignments. Cost: an Automation account on the Basic plan includes 500 free job minutes a month, and an action group with a runbook receiver is free; a run takes about a minute.

```
az login
./infra/killswitch/deploy.ps1 -AlertEmail you@example.com -WhatIf   # read-only preview
./infra/killswitch/deploy.ps1 -AlertEmail you@example.com
```

The script does two passes. Pass 1: resource group, Automation account with its system identity, runbook, custom role and the two assignments. It then uploads and publishes the runbook, deletes any previous webhook and asks Automation for a new webhook URI (`generateUri`). Pass 2: webhook (one-year expiry, parameters including `Mode=Kill`), action group, and the budget. After a change to `Stop-Watershed.ps1` or to the role, run it again: it updates the role in place and uploads and publishes the runbook again.

The budget is redeclared in full, because a budget holds at most five notifications and `hackathon-budget` already has five. After deployment it has: actual 25 %, 50 %, 80 % (kill switch), 90 % (kill switch) and forecasted 100 %, all e-mailed to `-AlertEmail`. The forecasted 50 % notification is the one dropped. The amount (50 CAD), monthly grain and dates (2026-10-01 to 2028-09-30) are unchanged; the e-mail address is a parameter so that it is not committed. The `-WhatIf` preview was run on 24/10/2026: the template is accepted by ARM.

### Test procedure

1. Dry run, no change: `./infra/killswitch/start-runbook.ps1` (Mode DryRun). The output lists `KEEP container app ca-web-<suffix>`, `WOULD stop container app ca-worker-<suffix>` (or `SKIP ... already stopped`), `SKIP job caj-replay-<suffix>: no running execution` (or `WOULD stop execution ...` during a replay), `WOULD disable subscription match-events/worker`, two `RESTORE-SETTINGS` lines and `WOULD delete Foundry deployment ...`. The previous runbook, which also stopped the web app, was run locally with a user token on 24/10/2026; this one has only run against the fakes of `tests/test_killswitch.py`.
2. Check the wiring without waiting for a real overspend: in the portal, action group `ag-killswitch-*` > Test action group, or open the budget and check that the 80 % and 90 % notifications list the action group. The Test action group button fires the real runbook in Kill mode: do it only together with step 3.
3. Real run, at a moment when the worker and the models can be down for ten minutes: `./infra/killswitch/start-runbook.ps1 -Mode Kill`. Check:
   - `az containerapp show -g rg-watershed-dev -n ca-worker-<suffix> --query properties.runningStatus` returns `Stopped`;
   - `az containerapp show -g rg-watershed-dev -n ca-web-<suffix> --query properties.runningStatus` has not changed and the public URL still answers;
   - `az servicebus topic subscription show -g rg-watershed-dev --namespace-name sb-<suffix> --topic-name match-events -n worker --query status` returns `Disabled`;
   - `az cognitiveservices account deployment list -g rg-watershed-foundry -n watershed-foundry-da -o table` is empty.
4. Restore: `./infra/killswitch/restore.ps1 -DryRun`, then `./infra/killswitch/restore.ps1`. It recreates `gpt-5.4-nano` (2026-03-17) and `gpt-5-mini` (2025-08-07), GlobalStandard, capacity 20, `Microsoft.DefaultV2`, `OnceCurrentVersionExpired`; sets the worker subscription back to Active; starts the worker app. If a listing fails, it stops with an error instead of reporting the restore as finished. Rerun the checks, then one card with the Writer to confirm the models answer (a few cents; skip if the Foundry budget is tight).

To stop the pipeline by hand in an emergency, step 3 is enough; it does not wait for the budget. To stop the web app as well: `az rest --method post --url "https://management.azure.com$(az containerapp show -g rg-watershed-dev -n ca-web-<suffix> --query id -o tsv)/stop?api-version=2024-03-01"`; the same call with `/start` brings it back (`restore.ps1` does not start it).

## Options not built

- **Deploy from CI with OIDC.** A GitHub Actions job with `azure/login` and a federated credential on a user-assigned identity (no secret stored in GitHub), restricted to `repo:akyuzdilhan/watershed:ref:refs/heads/main` or a `production` environment with a required reviewer, running `azd deploy`. Out of scope here: it would give the public repository's workflows a path to Azure, which needs the branch protection first and a role limited to the app resource group.
- **Kill switch on `foundry-budget`.** Foundry is the variable cost, so its 10 CAD budget would trigger earlier. Adding the action group to its `actual80` notification (portal: Cost Management > Budgets > foundry-budget > Alert conditions > Action group) reuses the same runbook.
- **Stop the web app too at 90 %.** A second webhook whose parameters ask the runbook to stop every container app, on the 90 % notification only, would bound a flood of traffic on the web app; the runbook has no such parameter yet.

## What remains

- **The kill switch is slow by design.** Cost Management data typically arrives 8 to 24 hours after usage, and budgets are evaluated on that data, so the kill can come a day or more after the spend. The real-time guards are the rate limits, the per-match call cap, the read-only console and Easy Auth; the kill switch only bounds a slow leak or an unattended weekend.
- **The web app keeps running after a kill.** It makes no model call and its compute is capped at two replicas of 0.25 vCPU and 0.5 GiB, but the Container Apps consumption plan also bills requests beyond a monthly free grant: a sustained flood keeps costing until the web app is stopped by hand (command above). The per-IP limit answers 429 early, and each answered request still counts.
- **After a restore in the same month**, the 80 % notification has already fired and will not fire again; only the 90 % one remains. Watch the cost by hand after a restore.
- **`azd up` after a kill** recreates nothing on the Foundry side (the deployments are outside azd) and may leave the worker app stopped: run `restore.ps1` rather than `azd up`.
- **Container images.** The web image runs as root: it listens on port 80, the port of the placeholder image of the first provision, and a non-root user would need a port above 1024 with the ingress `targetPort` changed with it. The worker image runs as user 10001. Neither image is scanned: a Trivy or Defender scan would be the next step.
- **Webhook expiry**: one year; rerun `deploy.ps1` before it expires if the project lives on.
- **Settings to run**: the GitHub commands above, and the kill switch deployment and test.
