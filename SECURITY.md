# Security policy

Watershed is a hackathon project maintained by one person. Reports are welcome and will be answered on a best-effort basis.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security > Report a vulnerability** on this repository (https://github.com/akyuzdilhan/watershed/security/advisories/new). Please do not open a public issue for a vulnerability. If private reporting is unavailable, open an issue that only asks for a private contact, without details.

Useful in a report: what you did, what you saw, the URL or file involved, and whether the public demo was affected.

## Scope

In scope:

- this repository's code, workflows and infrastructure templates (`infra/`);
- the public demo on Azure Container Apps, for issues such as access to the producer actions, ways to run up the Azure bill (model calls, scaling), injection into the generated texts, or exposure of data that should not be public.

Out of scope: denial of service by volume, social engineering, and vulnerabilities of Azure, GitHub or third-party packages themselves (report those to their owners).

Please do not run automated scanners or load tests against the demo: it runs on a small personal budget, and a kill switch stops it when the budget alert fires.

## What the project relies on

- **Synthetic data only.** Every match, player and event comes from the seeded generator in `src/generator/`. There is no personal data and no real match data.
- **No secrets in the repository.** Configuration comes from environment variables (`.env` is ignored; `.env.example` only holds placeholders). CI scans the whole history with gitleaks, and GitHub secret scanning with push protection is enabled.
- **Keyless Azure.** Service Bus and Cosmos DB have local (key) authentication disabled; the apps use managed identities and Azure roles, and the Foundry models are called with Microsoft Entra ID tokens. There is no connection string or API key to leak.
- **Cost guards.** Rate limits on the model deployments, a cap on model calls per match in the worker, budget alerts, and a kill switch (`infra/killswitch/`).
