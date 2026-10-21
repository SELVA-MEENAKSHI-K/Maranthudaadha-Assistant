# Design: Maranthudaadha Assistant

## Overview

The assistant is a Python 3.11 Telegram bot. Telegram is its only user interface. The running process accepts owner-only commands and messages, reads Google services through OAuth, keeps local state in SQLite, and schedules periodic jobs with the Telegram job queue/APScheduler dependencies. Strands Agents handles message understanding and email triage through the configured hosted Strands-compatible endpoint. Strands Decider makes the typed safety decision before a Calendar-write tool can run.

## Components

| Component | Responsibility |
| --- | --- |
| `main.py` | Load configuration, create storage and service clients, register Telegram handlers and scheduled jobs, then start polling or webhook mode. |
| `assistant/handlers.py` | Owner-only Telegram commands, task capture, Gmail decisions/replies, note commands, and reminder callbacks. |
| `assistant/capture.py` | Turn text or optional transcribed voice into a structured task; ask for missing information. |
| `assistant/email_intelligence.py` | Classify and summarize relevant mail using the Strands agent. |
| `assistant/gmail.py` | Read Gmail messages, track read state and dates, prepare replies, and send only after confirmation. |
| `assistant/google_calendar.py` | Read/create Calendar events and manage encrypted Google OAuth credentials. |
| `assistant/strands_runtime.py` | Configure the Strands agent with the hosted model endpoint. |
| `assistant/strands_calendar.py` | Provide the single Calendar-write tool and Decider-backed Strands intervention. |
| `decider_kit/` | Vendored typed Strands Decider client from the supplied hackathon kit; only the client/config required by this app are included. |
| `assistant/jobs.py` | Calendar synchronization, due reminders, Gmail scan, morning brief, and weekly review. |
| `assistant/store.py` | SQLite persistence for encrypted tokens, tasks, email decisions, reminders, and snooze state. |
| `assistant/notion_notes.py` | Optional Notion save/search integration. |
| `assistant/web.py` | Authenticated webhook endpoint for deployment. |

## Request and event flows

### Telegram input to Calendar event

1. Validate the sender against `TELEGRAM_USER_ID` before doing work.
2. Parse the owner's text or optional voice note into a task with title, local date/time, and priority.
3. Ask a short clarification if required details are missing.
4. Preview the exact event and collect Telegram confirmation, unless the owner explicitly requested direct creation.
5. Invoke a narrow Strands agent with one registered Calendar tool.
6. In `before_tool_call`, compare the proposed tool name and complete arguments against the prepared event, then ask Strands Decider whether the action is grounded and appropriate.
7. Deny mismatches, repeats, negative decisions, and service errors. On an uncertain decision, require explicit user confirmation.
8. The tool body independently checks the arguments and calls Google Calendar only after the intervention permits it.
9. Confirm success only after the Google API returns the created event.

### Gmail scan and deadline handling

1. A scheduled job or `/scanmail` reads messages matching the configured importance criteria.
2. Strands summarizes/classifies the selected message and identifies any explicit deadline evidence.
3. Telegram displays sender, subject, received date, read/unread state, category, and summary.
4. A clear deadline can be offered as a Calendar reminder; the Calendar write uses the same Decider guard.
5. An email reply is drafted and previewed separately. Gmail send happens only after the owner selects the explicit send action.
6. Before Gmail send, the kit's policy pattern checks only the outgoing draft text for credentials, threats/harassment, impersonation, or fraud. A clear violation blocks the send. A review result or unavailable Decider can be overridden only with a separate **Send anyway** action. The full source email and recipient address are not included in this policy request.

### Scheduled notifications

- Calendar sync reconciles upcoming events and their reminder schedule.
- Reminder polling sends due reminders with priority and Snooze/Done controls.
- The morning brief runs daily at 7:00 AM Asia/Kolkata.
- The weekly review runs Sunday at 8:00 PM Asia/Kolkata.
- Schedules require the bot process or deployed service to remain active.

## Data and configuration

- SQLite is the local source of truth for bot state, captured tasks, reminder delivery, and encrypted OAuth tokens.
- `data/` and database files are runtime data and are ignored by Git.
- `.env.example` documents names/defaults; `.env` holds private local settings and is ignored by Git.
- Google Calendar and Gmail access use the user's OAuth consent. Gmail send permission is required only for the explicit reply-send feature.
- `STRANDS_API_KEY` / `DECIDER_API_KEY` and endpoint/model settings configure hosted Strands. The project does not create AWS resources or call Bedrock.
- Gemini voice transcription and Notion notes are optional; absent credentials disable only those capabilities.

## Safety and failure behavior

- Identity checks guard every Telegram command, message, and callback.
- Calendar creation is fail-closed: Decider failure, malformed responses, missing evidence, mismatched arguments, or repeated tool proposals must not execute the Google write.
- The Gmail send action requires an explicit preview-and-confirm step.
- Provider output is treated as untrusted data. It cannot change the event approval boundary or the configured account identity.
- Never log or commit API keys, OAuth tokens, voice contents, or full email bodies unnecessarily.
- A failure in an optional integration must not silently report a successful save, send, or Calendar write.

## Kit pattern fit

- **Guardrails:** applied to Calendar tool execution through Strands `before_tool_call`; exact event validation and evidence rules are app-specific additions.
- **Policy:** applied before Gmail reply send. The request includes only the draft text and a short context label; the owner remains the final reviewer for an uncertain or unavailable policy result.
- **Tool selection:** Telegram command dispatch already selects the operation deterministically, and the Calendar action agent has only one tool. A second hosted selector would add a request without narrowing a real choice.
- **Context/memory:** SQLite task/reminder state and explicit Notion search already provide persistence/retrieval. Per-note Decider relevance scoring would spend one or more extra free answers per note and send more private content to the hosted service, so it is not enabled.
- **Model routing:** the supplied routing path selects among Bedrock models. It is excluded because this project uses the hosted Strands-compatible endpoint and does not use Bedrock/AWS credentials.

## Deployment shape

Local development uses long polling. A deployment may use the authenticated FastAPI webhook when the public base URL and webhook secret are configured. All credentials are environment variables; SQLite persistence on a hosted free tier depends on the host's disk persistence, so the deployment must use a persistent disk or accept that local state can reset on restart.

## Main risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Wrong or guessed event date | Clarify missing details, preview exact event, gate tool arguments and evidence with Decider, require confirmation when uncertain. |
| Decider/service outage | Fail closed; tell the owner no event was added. |
| Duplicate reminders | Reconcile reminders in SQLite and record delivery state. |
| Sensitive account data in source control | Ignore `.env`, OAuth files, database, and tokens; keep secrets in environment settings. |
| Hosted process sleeps or restarts | Explain that scheduled jobs require an active process and persistent local state requires durable storage. |
| Optional service missing | Disable that capability with an actionable status instead of blocking core commands. |
