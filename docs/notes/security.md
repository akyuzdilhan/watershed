# Security note: public repository and public demo URL

State read on 24/10/2026 with read-only `az` and `gh` calls. The repository `akyuzdilhan/watershed` is already public; GitHub secret scanning and push protection are already enabled on it; Dependabot alerts, Dependabot security updates, private vulnerability reporting and branch protection are not.

## Threats and what covers them

| # | Threat | Covered by | Status |
|---|---|---|---|
| 1 | A secret is committed (key, connection string, token) | No key exists: Service Bus, Cosmos DB and Foundry have local auth disabled (`disableLocalAuth: true`, checked on the Foundry account); apps use managed identities. `.env` is ignored. gitleaks in CI on the whole history; GitHub secret scanning and push protection | Done. gitleaks 8.30.1 run locally on the working tree and the 10 commits: no leak |
| 2 | A visitor runs up the model bill through the public URL | Per-deployment rate limits (20k tokens and 20 requests per minute each, GlobalStandard capacity 20); `max_calls_per_match=40` in `src/worker/desk.py` (beyond it, cards use the template); PUBLIC_MODE and rate limiting in the web app (web agent); Easy Auth on the producer side (infra agent); budget kill switch | Caps done; web and auth parts in progress elsewhere |
| 3 | Compute or platform costs grow without a model call | Container App `maxReplicas: 2`, `minReplicas: 0`; Log Analytics daily cap 0.2 GB; Cosmos DB free tier with 1000 RU/s shared and no autoscale; ACR Basic | Done |
| 4 | The cost alert arrives too late | Budget `hackathon-budget` (50 CAD, subscription) and `foundry-budget` (10 CAD, Foundry resource group) send e-mails; the kill switch stops the app at 80 % and again at 90 % actual | To deploy (below). Cost data lags (see "What remains") |
| 5 | Anyone uses the producer actions (approve, edit, reject) on the public demo | Easy Auth (infra agent) and PUBLIC_MODE read-only view (web agent) | In progress elsewhere |
| 6 | Prompt injection into the Writer | Inputs are synthetic and produced by the code, not by visitors; the Writer gets facts through `get_moment`; the deterministic Verifier rejects any number that is not claimed and matched, then falls back to a template | Done for the current design. Any free text from a visitor sent to the model would reopen it |
| 7 | A vulnerable or hijacked dependency or action | Dependabot version updates (pip at the root and in `src/web`, Docker, GitHub Actions, weekly); Dependabot alerts (to enable); actions pinned to major tags | Config done; alerts to enable |
| 8 | A workflow token or fork pull request is abused | `permissions: contents: read` at the top, `pull-requests: read` on the gitleaks job only, `persist-credentials: false`, no `pull_request_target`, no Azure login, no secret used | Done |
| 9 | Someone pushes or force-pushes to `main` | Branch protection (commands below) | To enable |
| 10 | The kill switch webhook URI leaks | It is a secret: passed as a `@secure()` parameter, never printed, not stored in deployment history. A leak can only stop the demo, not read data or spend money | Accepted |
| 11 | Personal data in the repository | Synthetic data only. The commit author e-mail is public in the history | See "What remains" |

Facts on the Foundry deployments come from `az cognitiveservices account deployment list`; the call cap from `src/worker/desk.py`; the other caps from `infra/resources.bicep`.

Worst case on the models, inference: two deployments at 20k tokens per minute can process at most about 58 million tokens a day. At output prices of the order of one to two US dollars per million tokens, that is tens of dollars a day, more than the whole budget in a day or two. The rate limits bound the damage per hour; the call cap and PUBLIC_MODE keep it near zero in normal use; the kill switch is the last net.

## GitHub settings for the human to run

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

| Target | Action | Why this one (Microsoft docs) |
|---|---|---|
| Container apps in `rg-watershed-dev` (web, and the worker if it becomes a container app) | `POST .../containerApps/{name}/stop` | Container Apps has Start and Stop operations (REST API, available in 2024-03-01). Stopping deactivates the app: no replica, nothing served, no compute billed. `start` brings back the same revision and ingress, so the restore needs no saved configuration. Disabling ingress would leave replicas running for the worker, and setting `maxReplicas` to 0 is not allowed |
| Service Bus topic subscriptions in `rg-watershed-dev` | `PUT` with `status: Disabled` | `EntityStatus` supports `Disabled` on subscriptions: nothing is delivered and nothing can be received. The runbook reads the subscription and sends its properties back, because a PUT replaces the resource and `requiresSession` cannot change after creation |
| Foundry deployments of `watershed-foundry-da` | Print settings (`RESTORE-SETTINGS` lines in the job output), then `DELETE` | A GlobalStandard deployment has a minimum capacity of 1, so it cannot be scaled to 0. Deleting is the only way to make every call fail. The models' settings are in `restore.ps1` and in the job output |

The runbook is Windows PowerShell 5.1, gets its token from the Automation managed identity endpoint (`IDENTITY_ENDPOINT`, `IDENTITY_HEADER`) and only calls the ARM REST API, so it needs no Az module. It discovers resources by listing them, so it does not depend on the `ob4grgftye7tm` suffix. Each step is independent; if one fails, the others still run and the job ends as Failed with the list of failures. Run by hand, `Mode` defaults to `DryRun`, which only prints what it would do; only the webhook sets `Mode=Kill`.

### Roles of the Automation identity

A custom role, `Watershed kill switch operator`, assignable only on `rg-watershed-dev` and `rg-watershed-foundry`, assigned on these two resource groups and nowhere else:

| Action | Used for |
|---|---|
| `Microsoft.App/containerApps/read`, `Microsoft.App/containerApps/stop/action` | List and stop the container apps |
| `Microsoft.ServiceBus/namespaces/read`, `.../topics/read`, `.../topics/subscriptions/read`, `.../topics/subscriptions/write` | List and disable the subscriptions |
| `Microsoft.CognitiveServices/accounts/deployments/read`, `.../deployments/delete` | Record and delete the deployments |

No `start` and no deployment `write`: the identity can stop, not restart or create, so a misuse of it cannot add cost. Built-in roles would be broader (Contributor on each resource group, or Container Apps Contributor plus Azure Service Bus Data Owner plus Cognitive Services Contributor). No data-plane role: the identity cannot read Cosmos DB or Service Bus messages. Automation local auth stays enabled because webhooks do not work without it.

### Deploy

Prerequisite: Owner (or User Access Administrator plus Contributor) on the subscription, to create the custom role and its assignments. Cost: an Automation account on the Basic plan includes 500 free job minutes a month, and an action group with a runbook receiver is free; a run takes about a minute.

```
az login
./infra/killswitch/deploy.ps1 -AlertEmail you@example.com -WhatIf   # read-only preview
./infra/killswitch/deploy.ps1 -AlertEmail you@example.com
```

The script does two passes. Pass 1: resource group, Automation account with its system identity, runbook, custom role and the two assignments. It then uploads and publishes the runbook, deletes any previous webhook and asks Automation for a new webhook URI (`generateUri`). Pass 2: webhook (one-year expiry, parameters including `Mode=Kill`), action group, and the budget.

The budget is redeclared in full, because a budget holds at most five notifications and `hackathon-budget` already has five. After deployment it has: actual 25 %, 50 %, 80 % (kill switch), 90 % (kill switch) and forecasted 100 %, all e-mailed to `-AlertEmail`. The forecasted 50 % notification is the one dropped. The amount (50 CAD), monthly grain and dates (2026-10-01 to 2028-09-30) are unchanged; the e-mail address is a parameter so that it is not committed. The `-WhatIf` preview was run on 24/10/2026: the template is accepted by ARM.

### Test procedure

1. Dry run, no change: `./infra/killswitch/start-runbook.ps1` (Mode DryRun). The output lists `WOULD stop container app ca-web-...`, `WOULD disable subscription match-events/worker`, two `RESTORE-SETTINGS` lines and `WOULD delete Foundry deployment ...`. The same logic run locally with a user token on 24/10/2026 gave exactly this.
2. Check the wiring without waiting for a real overspend: in the portal, action group `ag-killswitch-*` > Test action group, or open the budget and check that the 80 % and 90 % notifications list the action group. The Test action group button fires the real runbook in Kill mode: do it only together with step 3.
3. Real run, at a moment when the demo can be down for ten minutes: `./infra/killswitch/start-runbook.ps1 -Mode Kill`. Check:
   - `az containerapp show -g rg-watershed-dev -n ca-web-ob4grgftye7tm --query properties.runningStatus` returns `Stopped` and the public URL no longer answers;
   - `az servicebus topic subscription show -g rg-watershed-dev --namespace-name sb-ob4grgftye7tm --topic-name match-events -n worker --query status` returns `Disabled`;
   - `az cognitiveservices account deployment list -g rg-watershed-foundry -n watershed-foundry-da -o table` is empty.
4. Restore: `./infra/killswitch/restore.ps1 -DryRun`, then `./infra/killswitch/restore.ps1`. It recreates `gpt-5.4-nano` (2026-03-17) and `gpt-5-mini` (2025-08-07), GlobalStandard, capacity 20, `Microsoft.DefaultV2`, `OnceCurrentVersionExpired`; sets the subscriptions back to Active; starts the container apps. Rerun the three checks, then one card with the Writer to confirm the models answer (a few cents; skip if the Foundry budget is tight).

To stop everything by hand in an emergency, step 3 is enough; it does not wait for the budget.

## Options not built

- **Deploy from CI with OIDC.** A GitHub Actions job with `azure/login` and a federated credential on a user-assigned identity (no secret stored in GitHub), restricted to `repo:akyuzdilhan/watershed:ref:refs/heads/main` or a `production` environment with a required reviewer, running `azd deploy`. Out of scope here: it would give the public repository's workflows a path to Azure, which needs the branch protection first and a role limited to the app resource group.
- **Kill switch on `foundry-budget`.** Foundry is the variable cost, so its 10 CAD budget would trigger earlier. Adding the action group to its `actual80` notification (portal: Cost Management > Budgets > foundry-budget > Alert conditions > Action group) reuses the same runbook.

## What remains

- **The kill switch is slow by design.** Cost Management data typically arrives 8 to 24 hours after usage, and budgets are evaluated on that data, so the kill can come a day or more after the spend. The real-time guards are the rate limits, the per-match call cap, PUBLIC_MODE and Easy Auth; the kill switch only bounds a slow leak or an unattended weekend.
- **After a restore in the same month**, the 80 % notification has already fired and will not fire again; only the 90 % one remains. Watch the cost by hand after a restore.
- **`azd up` after a kill** recreates nothing on the Foundry side (the deployments are outside azd) and may leave the container app stopped: run `restore.ps1` rather than `azd up`.
- **Commit author e-mail.** Every commit carries `dilhan.akyuz72@gmail.com`, visible to anyone. To stop exposing it from now on: GitHub > Settings > Emails > Keep my email addresses private and Block command line pushes that expose my email, then `git config user.email <id>+akyuzdilhan@users.noreply.github.com`. Past commits keep it unless the history is rewritten.
- **Container image**: `python:3.12-slim` runs as root and is not scanned. A non-root `USER` in `src/web/Dockerfile` and a Trivy or Defender scan would be the next steps (web agent's files).
- **Webhook expiry**: one year; rerun `deploy.ps1` before it expires if the project lives on.
- **Settings to run**: the GitHub commands above, and the kill switch deployment and test.
