# Tasks: Maranthudaadha Assistant

This checklist records the implemented product phases and the remaining account/deployment setup. A checked item means the corresponding code/configuration is present; it does not claim a live provider account has been configured or that a test was run.

## Phase 1 — Calendar reminders and morning brief

- [x] 1.1 Add Telegram owner-ID checks, `/start`, persistent command menu, `/today`, and `/status`.
- [x] 1.2 Add Google OAuth storage with encrypted token persistence.
- [x] 1.3 Read Calendar events and reconcile reminder schedules.
- [x] 1.4 Send due reminders with priority and Snooze 1h / Tomorrow / Done actions.
- [x] 1.5 Send a 7:00 AM Asia/Kolkata daily brief.
- [ ] 1.6 Owner: configure Telegram and Google OAuth credentials locally, authorize the Google account, and confirm `/status` reports Calendar connected.

## Phase 2 — Gmail triage and replies

- [x] 2.1 Scan Gmail on schedule and with `/scanmail` for important categories.
- [x] 2.2 Summarize selected messages with received time and Read/Unread status.
- [x] 2.3 Offer a Calendar reminder for explicit email deadlines through the guarded Calendar flow.
- [x] 2.4 Draft and preview replies; require an explicit Send reply callback before Gmail send.
- [ ] 2.5 Owner: re-authorize Google OAuth with the required Gmail scopes and confirm the scan works with the owner's inbox.

## Phase 3 — Inbox capture and voice

- [x] 3.1 Extract title, date/time, and priority from typed or forwarded messages.
- [x] 3.2 Ask a brief follow-up when date/time is missing.
- [x] 3.3 Preview the prepared event and require confirmation unless the owner explicitly asks to add directly.
- [x] 3.4 Send the exact Calendar tool call through Strands `before_tool_call` and Strands Decider.
- [x] 3.5 Support optional Gemini transcription for Telegram voice notes.
- [ ] 3.6 Owner: add an optional Gemini key if voice transcription is needed and try a Tamil, Tanglish, or English voice note.

## Phase 4 — Notes, task actions, and weekly review

- [x] 4.1 Save and search notes through the optional Notion REST integration.
- [x] 4.2 Provide task listing/completion and reminder snooze/done actions.
- [x] 4.3 Send a Sunday 8:00 PM Asia/Kolkata review of completed/pending tasks and next week's events.
- [ ] 4.4 Owner: configure a Notion integration, share the notes data source, add the token and data source ID, and confirm `/notion`.

## Phase 5 — Supplied Strands Decider kit

- [x] 5.1 Vendor the reusable typed Decider client/configuration required by the assistant.
- [x] 5.2 Connect the client to the Calendar-write Strands intervention.
- [x] 5.3 Preserve exact-argument validation, confidence handling, request/rate limits, explicit confirmation, and fail-closed behavior.
- [x] 5.4 Include the Apache-2.0 license and attribution; exclude the separate AgentShield demo, Bedrock integration, and private kit files.
- [x] 5.5 Apply the kit's policy pattern to outgoing Gmail reply drafts before sending; block clear violations and require a separate owner override for review/unavailable results.
- [x] 5.6 Record why model-based tool selection, AgentCore memory, and Bedrock model routing are not enabled in this app.

## Phase 6 — Release and live operation

- [x] 6.1 Document Windows setup and usage in the root README.
- [x] 6.2 Exclude `.env`, OAuth credentials, local databases, and tokens from Git.
- [ ] 6.3 Owner: clone the GitHub repository, install Python 3.11 dependencies, and configure private environment values.
- [ ] 6.4 Owner: start `main.py`, send `/start`, and verify Calendar, Gmail, and optional integrations from Telegram.
- [ ] 6.5 Owner: if deploying to Render, configure environment variables, webhook settings, and persistent storage for SQLite.
- [ ] 6.6 Before a public release, complete manual acceptance checks from `requirements.md` and record any provider-specific limits or failures.
