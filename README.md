# Managed Virtual Services Time Log Automation

Google Chat -> Google Sheets time-log automation for Managed Virtual Services.

## Current config

- Chat space ID: `AAQAvfYSh9k` (`https://chat.google.com/app/chat/AAQAvfYSh9k`)
- Root Drive folder: `Managed Virtual Services Employee Timesheets`
- Owner transfer target: `ayel@managedvirtualservices.com`
- Direct share emails: `wrenz@managedvirtualservices.com`, `aggcomputers@aggdoors.com.au`, `ayel@aggdoors.com.au`
- Domain reader share: `managedvirtualservices.com`
- Posting back to Chat: disabled; the workflow only creates/updates Sheets.
- Schedule: daily at 4:20 PM Manila time, same as the AGG automation.

## Employee mapping

MVS uses separate Google Chat accounts, no shared account suffixes by default:

- Aliyah Ayco `<aliyah@managedvirtualservices.com>`
- Elaissa / DREWS VA Trainee `<drewsvatrainee@managedvirtualservices.com>`
- Wrenz Laylo `<wrenz@managedvirtualservices.com>`

If Google Chat hides display names and returns only stable `users/<id>` sender IDs, add those IDs to `SENDER_NAME_OVERRIDES` in `google_chat_time_log_summary.py` after a test read.

## OAuth note for `org_internal`

The normal Hermes OAuth app is restricted to its own organization. If `wrenz@managedvirtualservices.com` gets `Error 403: org_internal`, create a new Google Cloud OAuth Desktop client inside the Managed Virtual Services Google Workspace/Cloud project, then save the downloaded client file locally as `google_client_secret_mvs.json`.

Required APIs/scopes:

- Google Chat API
- Google Drive API
- Google Sheets API
- `https://www.googleapis.com/auth/chat.messages.readonly`
- `https://www.googleapis.com/auth/chat.memberships.readonly`
- `https://www.googleapis.com/auth/drive`
- `https://www.googleapis.com/auth/spreadsheets`

Local helper commands:

```bash
python setup_mvs_oauth.py --auth-url
python setup_mvs_oauth.py --auth-code "http://localhost:1/?state=...&code=..."
python setup_mvs_oauth.py --check
```

## Required GitHub secret

Set `GOOGLE_TOKEN_JSON` to the OAuth token JSON for `wrenz@managedvirtualservices.com` with the scopes above. The OAuth account must be a member of the Google Chat space.
