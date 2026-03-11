#!/bin/bash
mkdir -p ~/.flagentbot/workspace/skills
mkdir -p ~/.flagentbot/workspace/memory
mkdir -p ~/.flagentbot/workspace/sessions

# Copy skills into workspace
cp -r workspace/skills/* ~/.flagentbot/workspace/skills/ 2>/dev/null || true

# Copy cron configs
cp -r cron/ ~/.flagentbot/cron/ 2>/dev/null || true

# Generate config from env vars
cat > ~/.flagentbot/config.json << EOF
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
  },
  "tools": {
    "web": {
      "search": {
        "apiKey": "${BRAVE_SEARCH_API_KEY:-}"
      }
    }
  }
}
EOF

# Install and run
pip install -e .
nanobot gateway
