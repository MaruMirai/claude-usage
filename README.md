# Claude usage tracker

Tracks the 5-hour and weekly plan limits of several Claude accounts used with
Claude Code, says which account to use so the least of each week goes unused,
and switches Claude Code between them without logging in again.

Linux, Python 3 (standard library only), systemd user timers, `notify-send`.

## Install

    ./install.sh

Then `/login` once in Claude Code to each account you use. The tracker saves
each login the first time it sees it.

## Use

- `claude-usage open` – the dashboard: a card per account (each limit, its
  reset, full 5-hour sessions left to use this week) and a chart per account
  on one time scale, with resets marked. It refreshes itself every minute.
- `claude-switch [account|next|best]` – switch Claude Code to another saved
  login. Running sessions pick it up, as Claude Code reloads its credentials
  file when it changes.
- `claude-usage status` – one line, e.g. for Claude Code's status line.
- `claude-usage name <account> <nickname>` – rename an account.
- `claude-usage auto on|off` – switch by itself when the logged-in account
  runs out (on by default).

Notifications come at 80/95% of the 5-hour limit and 85/95% of the weekly
one, with a **Switch now** button, and when another account's week is closer
to going to waste.

## How it works

Every 3 minutes `tracker.py poll` asks Claude's usage endpoint (the numbers
`/usage` shows; asking uses none of the limit) for the logged-in account, and
for each other saved login while its access token is still good. It never
refreshes a token itself, as that would replace the refresh token Claude Code
holds.

Which account to use: of those with room in both limits now, the one with the
most weekly percent per hour still to use before its week resets, keeping to
the logged-in one unless another is clearly more pressing.

State, all private to you, is in `~/.local/state/claude-usage/`: `log.jsonl`
(readings), `accounts.json` (names), `logins/` (saved logins, mode 0600),
`dashboard.html`.

`./uninstall.sh` removes the timer and commands; the state folder stays.

The usage endpoint and the credentials file's layout are Claude Code's own and
undocumented, so a Claude Code update may change them.
