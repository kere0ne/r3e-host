# r3e hosting

r3e (by kers0nes) runs 24/7 on GitHub Actions. The workflow restarts the bot
every 6 hours (GitHub's job time limit), so expect a brief offline blip around
00:00, 06:00, 12:00 and 18:00 UTC.

Secrets (Settings > Secrets and variables > Actions):
- DISCORD_TOKEN: the bot token (required)
- USER_TOKEN: your Discord user token (optional, enables clone/forward in
  servers the bot is not in; /login also works but does not persist here)

Restart manually: Actions > r3e bot > Run workflow.
