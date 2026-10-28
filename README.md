# Linux Server

**A disposable, self-hosted bot server with a web control panel.** Run it from your own GitHub fork: GitHub Actions provisions an Ubuntu runner, starts your services, and replaces the runner on its scheduled cycle. Manage bots, logs, files, and Hermes from a browser; optionally connect by Tailscale SSH and Telegram.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

If this project is useful to you, please ⭐ [star the repository](https://github.com/ArashAtomic/linux-server)—it helps others discover it.

> **Know what you’re deploying:** this is an experimental GitHub Actions runner, not a permanent VPS or a guaranteed 24/7 hosting service. Runner replacement causes downtime; schedules, cancellations, cache restores, and external services can affect availability. The panel is published through a public Cloudflare Quick Tunnel URL. Use a strong password and don’t put sensitive production workloads here without independently reviewing and hardening the setup.

## Get started

### 1. Fork and enable Actions

Fork this repository to your GitHub account or organization. In your fork, open **Settings → Actions → General** and make sure GitHub Actions are allowed. Keep the `server.yml` workflow on the branch you intend to run.

### 2. Add repository secrets

In your fork, go to **Settings → Secrets and variables → Actions → Repository secrets → New repository secret**. Add secrets by name, without quotes or placeholder text.

**Required to start the server**

| Secret | What it’s for |
| --- | --- |
| `SERVER_PASSWORD` | Strong, unique password for the panel, SSH account, and 9Router dashboard. Do not use the default. |
| `HERMES_API_SERVER_KEY` | A strong random internal key used between the panel and Hermes; this is **not** an AI-provider key. |

Generate a key locally with `openssl rand -hex 32`, then save the output as `HERMES_API_SERVER_KEY`.

Hermes Agent is installed from a pinned upstream commit so runner setup is reproducible and is not affected by unverified upstream changes. The current pin works around an upstream web UI TypeScript build failure ([upstream report](https://github.com/NousResearch/hermes-agent/issues/134632)); update `HERMES_COMMIT` in `scripts/setup.sh` only after confirming a newer commit installs and builds successfully.

**Optional features (recommended when you plan to use them)**

| Secret | What it enables |
| --- | --- |
| `SERVER_USERNAME` | SSH account name. Defaults to `admin`. You may instead add this as an Actions variable under the **Variables** tab. The panel login itself asks for the password only. |
| `TAILSCALE_AUTHKEY` | Joins the runner to your tailnet for private SSH access. Configure this before relying on SSH. |
| `GH_PAT` | Lets the panel and status bot request an immediate redeploy. Give it permission to dispatch workflows in this repository (for a classic token, use the required `repo` and `workflow` scopes). |
| `STATUS_BOT_TOKEN` | Telegram bot token for startup notifications and server commands. Create a bot with [@BotFather](https://t.me/BotFather). |
| `OWNER_ID` | Numeric Telegram user ID authorized to use the status bot and default allowed Hermes user. Set it with the Telegram bot token(s) you enable. |
| `HERMES_TELEGRAM_BOT_TOKEN` | A **separate** Telegram bot token for Hermes chat. Do not reuse `STATUS_BOT_TOKEN`. Hermes uses `OWNER_ID` as its default allowed user if no separate allowlist is configured. |

You can put `SERVER_USERNAME` in Repository secrets as well; the workflow checks Actions variables first, then secrets. The status bot and Hermes Telegram bot are optional, but each needs its own token and `OWNER_ID`. AI-provider credentials are added later in the panel.

The startup notification lists each registered bot as running or stopped. Hermes Telegram does not send gateway shutdown/restart notices.

If upgrading an existing fork, create the `OWNER_ID` repository secret before deploying; the workflow no longer reads the old chat-ID secret name.

> **SSH safety:** SSH is intended to be reachable only over Tailscale. If Tailscale is missing or fails to connect, SSH may not be restricted to your tailnet. Don’t rely on SSH until you have verified the runner joined your tailnet and access is limited as intended.

### 3. Start the server

In your fork, open **Actions → Bot Server → Run workflow** and select the branch you want to deploy. The workflow also has a scheduled trigger; GitHub Actions must be enabled for the repository.

When startup completes:

- If configured, the status bot sends the panel URL and connection details to your Telegram chat.
- The workflow log also reports startup progress and the discovered panel URL. Open the **Bot Server** run in Actions to view it.
- The Cloudflare Quick Tunnel URL is temporary and can change when the runner is replaced. Sign in with `SERVER_PASSWORD`.

If the workflow fails, open its run and inspect the failing step and uploaded logs. The runner must finish its setup before the panel becomes available.

## Using the server

### Web control panel

The panel is the main interface. It provides server status, bot management, logs, environment-variable viewing, file browsing and previews, and Hermes Assistant. The panel login is password-only; use the `SERVER_PASSWORD` you configured.

To add a bot, open the bot-management area and provide:

1. Its GitHub repository URL.
2. A branch or ref and the Python entry-point file.
3. A GitHub personal access token only if the repository is private.
4. Any bot-specific environment settings as `KEY=value` lines.

The bot is cloned and installed on the runner. Its environment values are encrypted in the bot registry, but they are still sensitive data: only add bots and credentials you trust.

### Hermes Assistant and 9Router

In the Assistant panel, open **Providers**, choose an AI provider, enter its provider API key, fetch/select a model, and save. Hermes provider keys are separate from `HERMES_API_SERVER_KEY`; Hermes settings are restored from the Actions cache when available.

9Router runs locally on the runner and is not exposed as a public website. Its dashboard is available through an SSH port forward when Tailscale SSH is working:

```bash
ssh -L 20129:127.0.0.1:20128 <SERVER_USERNAME>@<TAILSCALE_IP>
```

While connected, open `http://localhost:20129/dashboard`. The dashboard password is `SERVER_PASSWORD`. Configure an upstream provider in 9Router, then select **9Router (local)** in Hermes Providers. 9Router is optional; it is not required to use other Hermes providers.

### Telegram and SSH

- The **status bot** is separate from Hermes and can report status, open the panel, show SSH connection details, or request a redeploy. Commands include `/status`, `/panel`, `/ssh`, `/redeploy`, `/help`, and `/start`.
- The **Hermes bot** is Hermes’ own Telegram integration and uses its own token. It is optional and configured at startup.
- For SSH, install and sign in to Tailscale on your device, ensure it is in the same tailnet, and use the command reported at startup (or `/ssh`). `SERVER_USERNAME` selects the SSH account.

## Runtime and data

- Each run uses a fresh GitHub-hosted Ubuntu runner. The workflow is scheduled for 00:45, 05:45, 10:45, 15:45, and 20:45 UTC, and also attempts to start a replacement after a successful run. Runs share a cancel-in-progress group, so a scheduled or manual run can cancel the currently active runner. Expect downtime; neither continuous availability nor immediate replacement is guaranteed.
- Bot registry data, Hermes state, and 9Router data are carried between runs using GitHub Actions caches. Caches are best-effort persistence, **not backups**; they may expire or fail to restore. Keep a separate backup of anything you cannot recreate.
- The runner’s temporary files and logs do not behave like storage on a permanent server.
- Actions caches and logs may contain sensitive service state. Limit repository access to people you trust, and don’t publish logs or cache contents.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Workflow stops during Hermes startup | Confirm `HERMES_API_SERVER_KEY` exists and is non-empty. |
| Panel URL is missing | Check the workflow’s startup/tunnel steps and logs; Cloudflare Quick Tunnels are external and can fail. |
| Redeploy action does not start a run | Check `GH_PAT` is present and has permission to dispatch workflows in this repository. |
| Status bot sends nothing | Check both `STATUS_BOT_TOKEN` and numeric `OWNER_ID`, and verify the bot can message that user/chat. |
| Hermes Telegram does not respond | Check `HERMES_TELEGRAM_BOT_TOKEN` and the allowed Telegram user ID; this token must differ from the status-bot token. |
| SSH is unavailable | Verify `TAILSCALE_AUTHKEY`, the runner’s tailnet connection, and your client’s Tailscale login. Do not expose SSH publicly as a workaround. |
| A bot or provider fails | Check its logs, repository/ref/entry point, environment values, provider credentials, and model access. Hermes health alone does not verify an AI provider key. |

## Project layout

```text
.github/workflows/server.yml   GitHub Actions deployment and lifecycle
scripts/                        Runner setup, startup, heartbeat, and shutdown
panel/app.py                    Flask API and process controls
panel/templates/                Web control panel and login
panel/run_hermes_gateway.sh      Hermes gateway supervisor
hermes/SOUL.md                  Hermes operator context
```

## Contributing

Issues and pull requests are welcome. Keep secrets out of commits and logs, and test changes without dispatching a workflow or modifying a live runner unless you explicitly intend to deploy.

## Credits

This project builds on the work of these projects and their contributors:

| Project | Use |
| --- | --- |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Assistant and Telegram gateway (MIT). |
| [9Router](https://github.com/decolua/9router) | Optional local AI gateway, run from the upstream Docker image. |
| [cloudflared](https://github.com/cloudflare/cloudflared) and [Tailscale](https://github.com/tailscale/tailscale) | Panel tunnel and private-network connectivity tools. |

These projects are independently maintained; their respective licenses and terms apply. This list highlights directly used tools and is not a complete inventory of their transitive dependencies or the GitHub-hosted runner's preinstalled software.

## License

This project is licensed under the [MIT License](./LICENSE), copyright © 2026 ArashAtomic. If you copy or redistribute this project, or substantial portions of it, include the copyright and permission notice from `LICENSE`. Keeping that notice is the MIT attribution requirement; a separate visible credit in every use is not required by MIT.
