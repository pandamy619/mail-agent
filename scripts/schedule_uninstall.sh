#!/bin/bash
# Снять расписание фоновой проверки почты.
# Запуск:  bash scripts/schedule_uninstall.sh
set -e
LABEL="com.vlad.mail-agent.check"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ Расписание снято."
