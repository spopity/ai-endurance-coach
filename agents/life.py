import os
import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = os.path.join(ROOT, "token.json")
CREDS_PATH = os.path.join(ROOT, "credentials.json")
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


def get_calendar_service():
    """Builds a Google Calendar service object from the stored token."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        # This agent doesn't handle re-authentication - it fails gracefully.
        return None

    try:
        return build('calendar', 'v3', credentials=creds)
    except HttpError as error:
        print(f"An error occurred building the service: {error}")
        return None


def run_life_agent(query: str):
    """Connects to Google Calendar and fetches upcoming events."""
    service = get_calendar_service()
    if not service:
        return "I'm sorry, but I can't access your Google Calendar. Please ensure your authentication token is valid."

    try:
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=10, singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        if not events:
            return "You have no upcoming events on your calendar."

        response_str = "Here are your next 10 upcoming events:\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            start_formatted = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).strftime('%a, %b %d at %I:%M %p')
            response_str += f"- {event['summary']} (Starts: {start_formatted})\n"
        return response_str
    except HttpError as error:
        return f"An error occurred with the Google Calendar API: {error}"
    except Exception as e:
        return f"An unexpected error occurred: {str(e)}"
