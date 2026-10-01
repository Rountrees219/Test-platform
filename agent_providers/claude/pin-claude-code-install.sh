#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CLAUDE_CODE_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/claude-code-version.txt")"

npm install -g --prefix /root/.local --force \
    --allow-scripts=@anthropic-ai/claude-code \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

installed_version="$(/root/.local/bin/claude --version | awk '{print $1}')"
if [[ "$installed_version" != "$CLAUDE_CODE_VERSION" ]]; then
    echo "expected Claude Code $CLAUDE_CODE_VERSION, found $installed_version" >&2
    exit 1
fi

echo "Claude Code pinned to $installed_version"
