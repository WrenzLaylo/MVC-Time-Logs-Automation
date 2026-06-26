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

## Required GitHub secret

Set `GOOGLE_TOKEN_JSON` to the OAuth token JSON for `wrenz@managedvirtualservices.com` with scopes:

- `https://www.googleapis.com/auth/chat.messages.readonly`
- `https://www.googleapis.com/auth/chat.memberships.readonly`
- `https://www.googleapis.com/auth/drive`
- `https://www.googleapis.com/auth/spreadsheets`

The OAuth account must be a member of the Google Chat space.
