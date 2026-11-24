#!/bin/sh
# Remove the timer and commands. Readings and saved logins stay in
# ~/.local/state/claude-usage/ (delete that folder to remove them too).
systemctl --user disable --now claude-usage.timer 2>/dev/null
rm -f "$HOME/.config/systemd/user/claude-usage.service" "$HOME/.config/systemd/user/claude-usage.timer" \
      "$HOME/.local/bin/claude-usage" "$HOME/.local/bin/claude-switch"
systemctl --user daemon-reload
