#!/bin/sh
# Install the tracker: a systemd user timer reading usage every 3 minutes,
# and the claude-usage and claude-switch commands in ~/.local/bin.
set -e
here=$(cd "$(dirname "$0")" && pwd)
units="$HOME/.config/systemd/user"
mkdir -p "$units" "$HOME/.local/bin"
cat > "$units/claude-usage.service" <<UNIT
[Unit]
Description=Read Claude plan usage for the logged-in account
[Service]
Type=oneshot
# Lets a notification's Switch button outlive the poll.
KillMode=process
ExecStart=/usr/bin/env python3 $here/tracker.py poll
UNIT
cat > "$units/claude-usage.timer" <<UNIT
[Unit]
Description=Read Claude plan usage every 3 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=3min
[Install]
WantedBy=timers.target
UNIT
ln -sf "$here/tracker.py" "$HOME/.local/bin/claude-usage"
cat > "$HOME/.local/bin/claude-switch" <<SH
#!/bin/sh
# Switch Claude Code to another saved login: claude-switch [account|next|best]
exec python3 "$here/tracker.py" switch "\$@"
SH
chmod +x "$here/tracker.py" "$HOME/.local/bin/claude-switch"
systemctl --user daemon-reload
systemctl --user enable --now claude-usage.timer
python3 "$here/tracker.py" poll
echo "Installed. Open the dashboard with: claude-usage open"
