"""Per-toolkit onboarding config: which auth config env holds the Composio auth config id and which triggers to
enable once an account is connected. Shared by the adapter (agent-driven onboarding) and scripts/composio_setup.py."""
from __future__ import annotations

TOOLKITS: dict[str, dict] = {
    "gmail": {
        "label": "Gmail",
        "auth_config_env": "COMPOSIO_GMAIL_AUTH_CONFIG_ID",
        "triggers": {"GMAIL_NEW_GMAIL_MESSAGE": {"labelIds": "INBOX", "interval": 1, "userId": "me"}},
    },
    "googlecalendar": {
        "label": "Google Calendar",
        "auth_config_env": "COMPOSIO_CALENDAR_AUTH_CONFIG_ID",
        "triggers": {"GOOGLECALENDAR_EVENT_STARTING_SOON_TRIGGER": {
            "calendarId": "primary", "minutesBeforeStart": 15, "countdownWindowMinutes": 5, "interval": 1, "includeAllDay": False}},
    },
    # Document services: search/read/create surfaces for the chat agent (adapters/composio/documents.py) and
    # profile samplers (profile.py). No triggers on purpose: their trigger types (NOTION_PAGE_CREATED/UPDATED,
    # GOOGLEDRIVE_FILE_CREATED/UPDATED, GOOGLEDOCS_DOCUMENT_UPDATED, …; listed 2026-09-11) fire on the person's
    # own edits — nothing worth an interruption. Slugs verified against the Composio API on 2026-09-11.
    "notion": {"label": "Notion", "auth_config_env": "COMPOSIO_NOTION_AUTH_CONFIG_ID", "triggers": {}},
    "googledrive": {"label": "Google Drive", "auth_config_env": "COMPOSIO_DRIVE_AUTH_CONFIG_ID", "triggers": {}},
    "googledocs": {"label": "Google Docs", "auth_config_env": "COMPOSIO_DOCS_AUTH_CONFIG_ID", "triggers": {}},
}
ALIASES = {"calendar": "googlecalendar", "google calendar": "googlecalendar", "takvim": "googlecalendar", "mail": "gmail", "e-mail": "gmail",
           "drive": "googledrive", "google drive": "googledrive", "gdrive": "googledrive",
           "docs": "googledocs", "google docs": "googledocs", "gdocs": "googledocs", "notion": "notion"}


def normalize_toolkit(name: str) -> str | None:
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in TOOLKITS else None
