#!/bin/bash
# Установка расписания фоновой проверки почты (этап 5) через launchd.
# Запуск:  bash scripts/schedule_install.sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
LABEL="com.vlad.mail-agent.check"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

INTERVAL_MIN=$("$PY" -c "import sys; sys.path.insert(0, '$PROJECT_DIR'); \
from agent import config; \
print(int(config.load().get('proactive', {}).get('check_interval_min', 15)))")

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$PROJECT_DIR/scripts/check_mail.py</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>StartInterval</key><integer>$((INTERVAL_MIN * 60))</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$PROJECT_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✅ Расписание установлено: проверка каждые $INTERVAL_MIN мин."
echo "   Первый запуск — прямо сейчас (создаст базовую линию, пинговать не будет)."
echo "   Если macOS спросит разрешение для python3 управлять Mail — разрешите."
echo "   Снять расписание: bash scripts/schedule_uninstall.sh"
