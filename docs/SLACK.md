# Slack setup

The agent posts each published scan into one Slack channel. That is all it does in Slack:
it reads no messages and has no Slack commands. Only a published scan posts (`--publish`,
or `DRY_RUN=0`); a dry run never does.

## What arrives

- **With a bot** (`SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID`): the HTML report, uploaded to
  the channel with one line naming the run ("NSO diagnostic report — <run id>"), or with a
  short digest when `NOTIFICATION_DIGEST=1`.
- **With a webhook** (`SLACK_WEBHOOK_URL`): the same line or digest, without the report. A
  webhook cannot attach files, so the message says the report is saved with the scan, or
  links to it when `DIAGNOSTIC_REPORT_BASE_URL` points at a server you run that serves the
  `runs/` folder.

When both bot settings are set, the bot is used and the webhook is not. With only one of
them, the webhook is used.

The settings go in `.env` in the checkout, or in `nso-diagnostic.env` beside
`docker-compose.yml` for Docker ([DOCKER.md](DOCKER.md)). `nso-diagnostic-run --configure`
asks for them: answer yes to "Send reports to Slack?", then `b` for a bot or `w` for a
webhook.

## Bot: uploads the report

1. Create the channel for the reports, for example `#nso-diagnostic-alerts`, or choose an
   existing one. A channel of their own keeps reports, which arrive with every scan, out of
   conversation.
2. At <https://api.slack.com/apps>, choose **Create New App** → **From scratch**, name it
   (for example `NSO Diagnostic Agent`) and pick the workspace.
3. Under **OAuth & Permissions** → **Bot Token Scopes**, add `files:write`. Optionally add
   `channels:read` (`groups:read` for a private channel) so that `--check-connection` can
   confirm the bot is in the channel.
4. On the same page, choose **Install to Workspace** → **Allow**, then copy the **Bot User
   OAuth Token** (it starts with `xoxb-`) into `SLACK_BOT_TOKEN`. Keep it as secret as a
   password.
5. Put the channel's ID into `SLACK_CHANNEL_ID`: open the channel and click its name; the
   ID, for example `C0123ABCD`, is at the bottom of the **About** tab. The channel name does
   not work.
6. Invite the bot to that channel with `/invite @your-app-name`. **The bot must be a member
   of the channel**; otherwise the upload fails with `not_in_channel`.

A scope added later takes effect once the app is installed again: Slack shows a banner
asking for it on the app's pages.

## Webhook: a notice without the report

1. In an app made as in steps 2–4 above, or the same app, turn on **Incoming Webhooks**.
2. Choose **Add New Webhook to Workspace**, pick the channel and **Allow**.
3. Copy the URL (`https://hooks.slack.com/services/…`) into `SLACK_WEBHOOK_URL`.

A webhook posts to the one channel picked for it; another channel needs another webhook.
Anyone with the URL can post to that channel, so keep it as secret as the bot token.

## When you are not a workspace admin

A workspace's admins can limit who creates channels and who installs apps. Then Slack
offers no channel to create, or sends an install request to the admins instead of
installing. In that case an admin does steps 1–4 and 6 of the bot setup and sends you the
token and the channel ID in a direct message, not in a channel. When the admin adds you
under **Basic Information** → **Collaborators**, you can manage the app afterwards.

## Checking it works

`nso-diagnostic-run --check-connection` checks the bot token without posting anything and,
when the bot has `channels:read` (or `groups:read`), that it is in the channel. A webhook
cannot be checked without posting, so it is only reported as configured. The command also
contacts NSO and, unless `--skip-llm` is given, the LLM endpoint.

To test a webhook, post a message to it:

```bash
set -a && source .env && set +a
curl -sS -X POST -H 'Content-type: application/json' \
  --data '{"text":"NSO diagnostic agent: Slack webhook test"}' "$SLACK_WEBHOOK_URL"
```

The answer `ok`, with the message in the channel, means it works.

A published scan prints `Published to: slack` (with `email` when that is set up too). When
Slack fails, it prints `Delivery failed: slack — <reason>` and ends with a non-zero exit
status; email is still sent and the reports are still saved.

## Problems

| What you see                                                          | What to do                                                                                        |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `not_in_channel`                                                      | Invite the bot: `/invite @your-app-name` in the channel                                           |
| `channel_not_found`                                                   | `SLACK_CHANNEL_ID` holds a name or a wrong ID, or the channel is private and the bot is not in it |
| `invalid_auth` or `token_revoked`                                     | Copy the **Bot User OAuth Token** again from **OAuth & Permissions**                              |
| `missing_scope` from an upload                                        | Add `files:write` (step 3) and install the app again                                              |
| `--check-connection`: "channel membership not checked"                | Optional: add `channels:read` (`groups:read` for a private channel) and install the app again     |
| `--check-connection`: "set both SLACK_BOT_TOKEN and SLACK_CHANNEL_ID" | One of the two bot settings is missing; until both are set, the webhook (if any) is used          |
| Webhook test answers `invalid_token` or `no_service`                  | The webhook was removed or the URL is wrong: add a new webhook                                    |
| Webhook test: `curl` complains of no URL                              | `.env` was not loaded in that shell: run the `set -a` line first                                  |
