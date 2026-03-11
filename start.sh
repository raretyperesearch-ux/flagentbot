#!/bin/bash
mkdir -p ~/.nanobot/workspace/skills
mkdir -p ~/.nanobot/workspace/memory

# Pre-populate workspace with our custom files BEFORE nanobot creates defaults
cp -f /app/workspace/SOUL.md ~/.nanobot/workspace/SOUL.md
cp -rf /app/workspace/skills/* ~/.nanobot/workspace/skills/
cp -rf /app/cron/* ~/.nanobot/workspace/cron/ 2>/dev/null || true

# Generate config
cat > ~/.nanobot/config.json << EOF
{
  "providers": {
    "anthropic": {
      "apiKey": "${ANTHROPIC_API_KEY}"
    }
  },
  "agents": {
    "defaults": {
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
nanobot gateway
