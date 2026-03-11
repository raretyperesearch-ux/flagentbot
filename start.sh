#!/bin/bash
# Generate config
mkdir -p ~/.nanobot
cat > ~/.nanobot/config.json << EOF
{
  "providers": {
    "anthropic": {
      "apiKey": "${ANTHROPIC_API_KEY}"
    }
  },
  "agents": {
    "defaults": {
      "workspace": "/app/workspace",
      "model": "claude-sonnet-4-20250514",
      "provider": "anthropic",
      "maxTokens": 4096
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "${TELEGRAM_BOT_TOKEN}",
      "allowFrom": ["*"]
    }
  }
}
EOF

pip install -e .

# Start nanobot in background
nanobot gateway &
NANOBOT_PID=$!

# Wait for nanobot to create default workspace files
sleep 5

# NOW overwrite with our custom files
cp -f /app/workspace/SOUL.md ~/.nanobot/workspace/SOUL.md 2>/dev/null || true
cp -rf /app/workspace/skills/* ~/.nanobot/workspace/skills/ 2>/dev/null || true
cp -rf /app/cron/* ~/.nanobot/workspace/cron/ 2>/dev/null || true

echo "✓ Custom SOUL.md and skills injected"

# Wait for nanobot process
wait $NANOBOT_PID
