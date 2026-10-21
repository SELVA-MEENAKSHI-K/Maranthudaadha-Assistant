# Requirements: Maranthudaadha Assistant

## Purpose

Maranthudaadha Assistant is a private, Telegram-only personal assistant that helps its owner remember meetings, deadlines, and important messages. It uses Google Calendar and Gmail for connected account data, Strands Agents for language understanding, Strands Decider for Calendar-write safety decisions, and SQLite for local state.

## Users and scope

- The only supported user is the Telegram account whose numeric ID is configured in `TELEGRAM_USER_ID`.
- The primary time zone is `Asia/Kolkata`.
- Chat replies should follow the user's language and style (Tamil, Tanglish, or English) and stay brief.
- The bot does not use Amazon Bedrock or AWS account credentials.
- Notion and Gemini voice transcription are optional integrations; core text, Calendar, and Gmail functions must remain usable without them.

## Functional requirements

### 1. Private Telegram interface

**User story:** As the owner, I want one Telegram chat to be the assistant's interface so my personal information is not exposed to other users.

- **1.1** The bot shall process commands, messages, and callback buttons only when the sender ID equals `TELEGRAM_USER_ID`.
- **1.2** On `/start`, the bot shall show a welcome message and its command menu.
- **1.3** The command menu shall remain available across chat sessions and include the supported actions.
- **1.4** The bot shall acknowledge callback queries before doing potentially slow work and return a short result or error.

### 2. Google Calendar and reminders

**User story:** As the owner, I want upcoming events and deadlines surfaced before they happen.

- **2.1** The assistant shall read upcoming events from the connected Google Calendar.
- **2.2** It shall send a daily brief at 7:00 AM Asia/Kolkata with today's events and recent important email summaries when available.
- **2.3** It shall schedule reminders for supported lead times before each event, including one day, three hours, and 30 minutes; higher-priority events may also use additional lead times.
- **2.4** A reminder shall show the event title, local time, priority, and actions to snooze for one hour, snooze until tomorrow, or mark done.
- **2.5** `/today` shall list today's events. `/status` shall report Google connection status.

### 3. Safe Calendar event creation

**User story:** As the owner, I want captured events added accurately and only after an appropriate approval check.

- **3.1** The assistant shall ask for confirmation before creating or changing an event unless the owner explicitly asks to add it directly.
- **3.2** Every Calendar write shall pass through a Strands `before_tool_call` intervention backed by Strands Decider.
- **3.3** The intervention shall reject tool calls that do not exactly match the prepared event, are repeated, lack evidence/approval, or receive a negative decision.
- **3.4** A confident Decider approval may allow a qualifying automatic reminder from an important email only when the email contains an explicit future date and the source evidence is retained for review.
- **3.5** An uncertain decision shall require the owner's explicit confirmation or be denied. A Decider/network/configuration failure shall fail closed and create no Calendar event.
- **3.6** The assistant shall report success only after Google Calendar confirms event creation.

### 4. Gmail triage and replies

**User story:** As the owner, I want important messages summarized with enough context to decide what to do next.

- **4.1** The assistant shall scan Gmail periodically and on `/scanmail` for messages relevant to exams, college, GDG, internships, deadlines, and interviews.
- **4.2** Each surfaced message shall show its subject, sender, received date/time, read/unread state, category, and a concise summary.
- **4.3** When a message contains a clear deadline, the assistant shall offer a Yes/No action to add a Calendar reminder.
- **4.4** Gmail reply shall be a separate action from Telegram's quote-reply feature. The assistant shall collect a draft, show its preview, and send only after an explicit Send confirmation.
- **4.5** A Gmail scan or reply error shall produce a concise status message without claiming the operation succeeded.
- **4.6** Before sending a Gmail draft, the assistant shall run a Strands Decider policy review against a limited safety policy. A blocked draft shall not be sent; an uncertain/review result or unavailable review may proceed only after the owner explicitly chooses **Send anyway**.

### 5. Inbox capture and voice

**User story:** As the owner, I want to forward or dictate a reminder in natural language instead of filling out a form.

- **5.1** The assistant shall extract a task title, date/time, and priority from typed or forwarded text.
- **5.2** If required date/time details are missing or ambiguous, it shall ask one short clarifying question before preparing a Calendar event.
- **5.3** A complete event shall be shown for confirmation unless the owner explicitly says to add it directly.
- **5.4** When an optional Gemini key is configured, Tamil, Tanglish, and English Telegram voice notes shall be transcribed and handled like typed messages.
- **5.5** The assistant shall send a short confirmation after successful capture or explain which detail is still needed.

### 6. Tasks, notes, and weekly review

**User story:** As the owner, I want captured work and notes to remain searchable and easy to review.

- **6.1** `/tasks` shall list pending captured tasks with date/time and priority when available.
- **6.2** `/note <text>` shall save a note to the configured Notion data source; `/searchnotes <words>` shall search it.
- **6.3** If Notion is not configured, the bot shall explain which integration settings are missing without claiming a note was saved.
- **6.4** Each Sunday at 8:00 PM Asia/Kolkata, the assistant shall summarize tasks completed and pending during the week and key events for the next week.

### 7. Configuration, privacy, and deployment

- **7.1** API keys, OAuth configuration, bot tokens, and deployment secrets shall be supplied through environment variables and excluded from version control.
- **7.2** OAuth tokens stored locally shall be encrypted using the configured token-encryption key.
- **7.3** SQLite shall store local task, reminder, and assistant state; runtime database files shall not be committed.
- **7.4** The project shall support local polling and a configured webhook deployment path.
- **7.5** Optional integrations shall fail with clear guidance while unrelated assistant features remain available.

## Acceptance checks

1. An unauthorized Telegram account receives no assistant functionality.
2. `/start`, `/today`, `/tasks`, `/status`, and `/scanmail` return the expected menu or status.
3. A typed event is previewed and confirmed before Calendar creation; changing a proposed tool argument blocks the write.
4. A denied, uncertain, unavailable, or malformed Decider result never creates a Calendar event.
5. Important mail summaries include received time and read state; sending an email requires a separate explicit confirmation.
6. Missing task date/time triggers a clarification; optional voice and Notion configuration do not prevent core startup.
7. Reminder actions, the morning brief, and Sunday review use Asia/Kolkata time.
8. A clean GitHub checkout contains no `.env`, OAuth credentials, tokens, or runtime database.
