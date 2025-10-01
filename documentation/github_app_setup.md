# GitHub App Setup Guide for Orchestrator Bot

This guide walks you through creating a GitHub App for the orchestrator, which enables proper bot identification, better permissions management, and access to GitHub's bot-specific features.

## Why Use a GitHub App?

- Comments appear as `orchestrator-bot[bot]` with a bot badge
- `isBot` flag is properly set to `true`
- Better rate limits (5000 req/hour per installation)
- Granular permissions model
- Easier to manage and revoke access
- Professional appearance

## Step-by-Step Setup

### 1. Create the GitHub App

1. **Go to GitHub App settings:**
   - Personal account: https://github.com/settings/apps/new
   - Organization: https://github.com/organizations/YOUR_ORG/settings/apps/new

2. **Fill in basic information:**
   - **GitHub App name**: `Orchestrator Bot` (or your preferred name)
   - **Description**: `Autonomous AI agent orchestrator for software development workflows`
   - **Homepage URL**: Your repository URL or website
   - **Webhook URL**: Leave blank (we don't need webhooks for now)
   - **Webhook secret**: Leave blank

3. **Set permissions:**

   **Repository permissions:**
   - **Issues**: Read and write
   - **Pull requests**: Read and write
   - **Contents**: Read only (for accessing repo files)
   - **Metadata**: Read only (automatically selected)

   **Organization permissions:**
   - None needed

   **Account permissions:**
   - None needed

4. **Subscribe to events:**
   - None needed (we're using polling, not webhooks)

5. **Where can this GitHub App be installed?**
   - Select **"Only on this account"** (makes it private)

6. **Click "Create GitHub App"**

### 2. Generate Private Key

1. After creating the app, scroll down to **"Private keys"** section
2. Click **"Generate a private key"**
3. A `.pem` file will download automatically
4. **Save this file securely** - you'll need it for authentication
5. Recommended location: `~/.orchestrator/orchestrator-bot.pem`

### 3. Note Your App Details

After creation, note these values from the app settings page:

- **App ID**: Found at the top of the settings page (e.g., `123456`)
- **Client ID**: Found in the settings (not needed for our use case)
- **Private key**: The `.pem` file you downloaded

### 4. Install the App on Your Repositories

1. On the app settings page, click **"Install App"** in the left sidebar
2. Select your account/organization
3. Choose repositories:
   - **All repositories** (simpler)
   - OR **Only select repositories** (more secure - select the repos you want the orchestrator to access)
4. Click **"Install"**
5. **Note the Installation ID** from the URL after installation:
   - URL will be like: `https://github.com/settings/installations/12345678`
   - The number at the end (`12345678`) is your Installation ID

### 5. Configure the Orchestrator

Create or update your `.env` file with the GitHub App credentials:

```bash
# GitHub App Authentication (preferred)
GITHUB_APP_ID=123456
GITHUB_APP_INSTALLATION_ID=12345678
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/orchestrator-bot.pem

# OR provide the private key directly (not recommended for production)
# GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"

# Legacy PAT (will be used as fallback if GitHub App not configured)
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
GITHUB_ORG=your-org-or-username
```

### 6. Verify the Setup

After configuring the orchestrator, you can verify the setup:

```bash
# In the orchestrator container
docker-compose exec orchestrator python -c "
from services.github_app_auth import GitHubAppAuth
auth = GitHubAppAuth()
token = auth.get_installation_token()
print(f'Successfully authenticated as GitHub App!')
print(f'Token: {token[:20]}...')
"
```

## Security Best Practices

1. **Keep the private key secure:**
   - Never commit the `.pem` file to git
   - Use restrictive file permissions: `chmod 600 orchestrator-bot.pem`
   - Store in a secure location outside the repo

2. **Add to .gitignore:**
   ```
   *.pem
   .env
   ```

3. **Rotate keys periodically:**
   - GitHub allows multiple private keys
   - Generate a new key, update config, then delete old key

4. **Use minimal permissions:**
   - Only grant what the orchestrator needs
   - Review and audit permissions regularly

## Troubleshooting

### "Bad credentials" error
- Check that APP_ID and INSTALLATION_ID are correct
- Verify the private key is properly formatted
- Ensure the app is installed on the target repository

### "Resource not accessible" error
- Check that the app has the required permissions
- Verify the app is installed on the correct repositories
- Check that the installation wasn't revoked

### Comments still appear as your user account
- Verify the orchestrator is using GitHub App auth (check logs)
- Ensure GITHUB_APP_* env vars are set correctly
- Restart the orchestrator after configuration changes

## Migration from PAT to GitHub App

The orchestrator will automatically use GitHub App authentication if configured, falling back to PAT if not. You can run both simultaneously during migration:

1. Set up GitHub App (steps above)
2. Configure environment variables
3. Restart orchestrator
4. Verify bot comments appear with bot badge
5. Remove GITHUB_TOKEN from environment once verified

## Advanced Configuration

### Using GitHub App in Docker

Mount the private key as a volume:

```yaml
services:
  orchestrator:
    volumes:
      - ~/.orchestrator/orchestrator-bot.pem:/app/secrets/github-app.pem:ro
    environment:
      - GITHUB_APP_PRIVATE_KEY_PATH=/app/secrets/github-app.pem
```

### Multiple Installations

If you want the orchestrator to work across multiple organizations:

1. Install the app in each organization
2. Note each installation ID
3. Configure multiple installations in the orchestrator (future feature)

## Next Steps

After setup:
- Comments will appear as `orchestrator-bot[bot]`
- The `isBot` flag will be properly set
- You can use bot-specific GitHub features
- Better rate limits and audit logging

## References

- [GitHub Apps Documentation](https://docs.github.com/en/apps)
- [Authenticating with GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app)
- [GitHub App Permissions](https://docs.github.com/en/rest/overview/permissions-required-for-github-apps)