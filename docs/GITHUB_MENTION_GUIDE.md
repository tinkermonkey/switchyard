# How to Mention the Orchestrator Bot

## The Problem

GitHub Apps don't provide @ mention autocomplete like user accounts do. When you type `@`, only users appear in the dropdown, not bots.

## Solutions

### Option 1: Type Manually (Current)

Just type: `@orchestrator-bot`

The orchestrator detects any of these variations:
- `@orchestrator-bot` ✅
- `@orchestrator-bot[bot]` ✅ (GitHub's official bot name format)

**No autocomplete, but it works!**

### Option 2: Create a Keyboard Shortcut

**On Mac**:
1. System Settings → Keyboard → Text Replacements
2. Replace: `@orc` → `@orchestrator-bot`
3. Now typing `@orc` autocompletes to `@orchestrator-bot`

**On Windows**:
1. Use AutoHotkey or Windows built-in text replacement
2. `@orc` → `@orchestrator-bot`

### Option 3: Use a Slash Command (Alternative)

We could add support for slash commands like:
- `/orchestrator elaborate on X`
- `/orc refine this section`

But @mentions are more GitHub-native.

### Option 4: Browser Extension

Create a simple Chrome/Firefox extension that adds the bot to autocomplete.

## Why No Autocomplete?

GitHub's @ mention autocomplete only shows:
1. **Users** in the organization
2. **Teams** in the organization
3. **NOT** GitHub Apps or bots

This is a GitHub limitation, not our configuration.

## Current Best Practice

**Just type `@orchestrator-bot` manually**. It's 17 characters and works everywhere:
- Issues
- Pull requests
- Discussions
- Discussion replies (threaded)

The bot will detect it and respond!

## Future Enhancement

We could add alternative triggers:
- Keywords: "hey bot", "orchestrator", etc.
- Emojis: 🤖 at start of comment
- Slash commands: `/orchestrator`

But @mentions are the most intuitive for GitHub users.
