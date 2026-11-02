import os
import base64
import requests
import time
import json
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field
import streamlit as st
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar'
]

CACHE_FILE = "cached_events.json"

def load_app_config():
    """Loads local profile configuration for children and grade aliases."""
    if os.path.exists("config.json") and os.path.getsize("config.json") > 0:
        try:
            with open("config.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    # Fallback default if config doesn't exist yet
    return {
        "children": [
            {"name": "Child 1", "grade": "Class 3", "aliases": ["3", "Class III", "grade iii", "3rd"]},
            {"name": "Child 2", "grade": "Class 1", "aliases": ["1", "Class I", "grade i", "1st"]}
        ]
    }

def check_work_conflicts(calendar_service, start_str, end_str):
    """Queries Google Calendar ensuring RFC3339 format compliance for conflict detection."""
    try:
        if not start_str:
            return {"summaries": [], "items": []}
            
        ny_tz = ZoneInfo("America/New_York")
        
        # Helper to parse and format datetime with the correct local offset
        def format_to_rfc3339(dt_str, is_end=False):
            if not dt_str:
                return None
            if len(dt_str) == 10:  # All-day event (YYYY-MM-DD)
                time_part = "23:59:59" if is_end else "00:00:00"
                dt_str = f"{dt_str}T{time_part}"
            
            # Strip any existing trailing 'Z' or offsets to normalize
            clean_str = dt_str.rstrip('Z').split('+')[0].split('-')[0:3] # basic cleanup
            # Parse as naive datetime, then localize to New York
            # If string already has seconds/microseconds, parse accordingly:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(dt_str[:19], fmt)
                    break
                except ValueError:
                    continue
            else:
                dt = datetime.fromisoformat(dt_str[:19])
                
            # Attach New York timezone (automatically handles EDT vs EST)
            localized_dt = dt.replace(tzinfo=ny_tz)
            return localized_dt.isoformat()

        time_min = format_to_rfc3339(start_str, is_end=False)
        time_max = format_to_rfc3339(end_str or start_str, is_end=True)

        #print(f"🔍 Querying Calendar API (NY Time) -> min: {time_min} | max: {time_max}")

        events_result = calendar_service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        existing_items = events_result.get('items', [])
        summaries = [ev.get('summary', 'Busy Event') for ev in existing_items]
        
        return {"summaries": summaries, "items": existing_items}
    
    except Exception as e:
        print(f"Error checking calendar conflicts: {e}")
        return {"summaries": [], "items": []}

def get_or_fetch_events(gmail_service, calendar_service, start_date,end_date):
    """Loads from local cache if valid, otherwise fetches live, extracts links, and checks conflicts."""
    if os.path.exists(CACHE_FILE) and os.path.getsize(CACHE_FILE) > 0:
        try:
            print("⚡ Loading events from local cache file (0 API calls)...")
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("⚠️ Cache file malformed. Fetching fresh data...")

    print("🌐 Fetching fresh events from Gmail and Gemini API...")
    extracted_events = extract_events_from_gmail(gmail_service, start_date, end_date)
    event_dicts = []
    for event in extracted_events:
        start_time = event.start_datetime
        end_time = event.end_datetime or event.start_datetime
        
        # 1. Query calendar for existing items in this time slot
        conflict_data = check_work_conflicts(calendar_service, start_time, end_time)
        conflicts = conflict_data["summaries"]
        existing_items = conflict_data["items"]
        
        # 2. Check if this exact event title is already on the calendar (skips ongoing/transparent items)
        title_lower = event.title.strip().lower()
        already_exists = any(
            title_lower in item.get('summary', '').strip().lower() 
            for item in existing_items
        )
        
        if already_exists:
            print(f"⏩ Skipping '{event.title}' — already exists on Google Calendar.")
            continue
        
        event_dicts.append({
            "title": event.title,
            "category": event.category,
            "description": event.description,
            "start": start_time,
            "end": end_time,
            "prep": event.prep_notes or "None",
            "rsvp_link": getattr(event, 'rsvp_link', None),
            "info_link": getattr(event, 'info_link', None),
            "is_blocker": getattr(event, 'is_blocker', True),
            "sub_events": [sub.model_dump() for sub in getattr(event, 'sub_events', [])], 
            "conflicts": conflicts
        })
    
    with open(CACHE_FILE, "w") as f:
        json.dump(event_dicts, f, indent=4)
        
    return event_dicts

class SubEvent(BaseModel):
    title: str = Field(description="Specific session or class window, e.g., 'Grade 3 Book Fair Visit'")
    start_datetime: Optional[str] = Field(None, description="ISO 8601 start time")
    end_datetime: Optional[str] = Field(None, description="ISO 8601 end time")
    target_grade: Optional[str] = Field(None, description="Specific grade targeted, e.g., 'Grade 1', 'Grade 3', or 'All'")
    description: Optional[str] = Field(None, description="Details or requirements for this specific slot")

class SchoolEvent(BaseModel):
    title: str = Field(description="Title or short description of the school event")
    start_datetime: str = Field(description="ISO 8601 start time, e.g. 2026-11-05T14:00:00")
    end_datetime: str = Field(description="ISO 8601 end time")
    description: str = Field(description="Brief explanation of the event context")
    is_all_day: bool = Field(False, description="True if this spans a full day or multiple days without a specific hour")
    requires_prep: bool
    prep_notes: Optional[str] = Field(description="Specific preparation required, e.g. 'Buy canned goods', 'Prepare costume', 'None'")
    category: str = Field(description="Use 'academic' for closures/half-days/conferences, 'activity' for plays/concerts/fairs, or 'drive_prep' for food drives, costume days, or items requiring prep.")
    rsvp_link: Optional[str] = Field(None, description="Direct URL for RSVP if present in text")
    info_link: Optional[str] = Field(None, description="Direct URL or reference link for further event details")
    is_blocker: bool = Field(
        #default=True, 
        description="True if the event requires dedicated calendar time (e.g. conference, meeting). False if passive/ongoing drop-off drive (e.g. candy drive, box tops)."
    )
    sub_events: List[BaseModel] = Field(default=[], description="Nested rows from a schedule table or class-specific visit windows.")

    @property
    def date(self) -> str:
        """Safely extract YYYY-MM-DD for UI display and legacy checks."""
        return self.start_datetime.split("T")[0] if self.start_datetime else ""

    
class NewsletterExtraction(BaseModel):
    events: List[SchoolEvent]

def add_to_google_calendar(calendar_service, event):
    """Standalone function to push an event to Google Calendar."""
    event_body = {
        'summary': event.title,
        'description': f"{event.description}\n\nPrep Notes: {event.prep_notes or 'None'}",
    }
    
    if event.is_all_day:
        end_date = event.end_datetime.split("T")[0] if event.end_datetime else event.date
        event_body['start'] = {'date': event.date, 
                               'timeZone':'America/New_York'}
        event_body['end'] = {'date': end_date,
                             'timeZone':'America/New_York'}
    else:
        event_body['start'] = {'dateTime': event.start_datetime, 
                               'timeZone': 'America/New_York'}
        event_body['end'] = {'dateTime': event.end_datetime or event.start_datetime, 
                             'timeZone': 'America/New_York'}

    try:
        created_event = calendar_service.events().insert(calendarId='primary', body=event_body).execute()
        print(f"Event created: {created_event.get('htmlLink')} ({event.title})")
        return True
    except Exception as e:
        print(f"Error adding event to calendar: {e}")
        return False


def get_google_services():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds), build('calendar', 'v3', credentials=creds)

# def clean_html(html_content):
#     soup = BeautifulSoup(html_content, 'html.parser')
#     return soup.get_text(separator='\n')



def clean_html(html_content):
    if not html_content:
        return ""
    
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Remove clutter tags
    for tag in soup(["script", "style", "head", "meta", "title"]):
        tag.decompose()
        
    # Convert <a> tags to Markdown [link_text](url) format
    for a in soup.find_all("a", href=True):
        url = a["href"].strip()
        text = a.get_text(strip=True) or url
        if url.startswith("http"):
            a.replace_with(f" [{text}]({url}) ")
            
    return soup.get_text(separator="\n", strip=True)



def extract_events_from_gmail(gmail_service,start_date, end_date):

    max_retries = 3
    retry_delay = 2
    response = None

    formatted_start = start_date.strftime("%Y/%m/%d")
    formatted_end_exclusive = (end_date + timedelta(days=1)).strftime("%Y/%m/%d")

    # Combine subject keywords and date range
    query = f"subject:Class subject:(update OR weekly OR newsletter OR summary) after:{formatted_start} before:{formatted_end_exclusive}"

    #query = f"subject:Class subject:(update OR weekly OR newsletter OR summary)"
    results = gmail_service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    print(f"[DEBUG] Found {len(messages)} recent message(s) in this account.")
    # Helper function to recursively find email body in nested parts
    def get_email_body(payload):
        body = ""
        if 'body' in payload and 'data' in payload['body']:
            data = payload['body']['data']
            if data:
                try:
                    body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                except Exception:
                    pass
        if not body and 'parts' in payload:
            for part in payload['parts']:
                mime_type = part.get('mimeType', '')
                if mime_type in ['text/html', 'text/plain']:
                    data = part.get('body', {}).get('data')
                    if data:
                        try:
                            body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                            if mime_type == 'text/html':
                                break
                        except Exception:
                            pass
                if 'parts' in part:
                    nested_body = get_email_body(part)
                    if nested_body:
                        body = nested_body
        return body


    
    for msg in messages:
        msg_data = gmail_service.users().messages().get(
            userId='me', id=msg['id'], format='metadata', metadataHeaders=['Subject', 'From']
        ).execute()
        headers = msg_data.get('payload', {}).get('headers', [])
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
        print(f"  -> Subject: '{subject}' | From: '{sender}'")
        
    print("[DEBUG] -------------------------")
    #return []
    
    all_extracted_events = []
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    current_date = datetime.now().strftime("%Y-%m-%d")

    for msg in messages:
        print(f"[DEBUG] Fetching full content for message ID: {msg['id']}")
        msg_data = gmail_service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        headers = msg_data.get('payload', {}).get('headers', [])
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
        print(f"[DEBUG] Checking subject: '{subject}'")
        sub_lower = subject.lower()
        if "class" in sub_lower and any(kw in sub_lower for kw in ["update", "weekly", "newsletter","summary"]):
            print(f"[DEBUG] ✅ Match confirmed! Processing body for: {subject}")
            
            payload = msg_data.get('payload', {})
            raw_body = get_email_body(payload)
            
            clean_text = clean_html(raw_body)
            print(f"[DEBUG] Cleaned text length: {len(clean_text)} characters.")

            prompt = f"""
            Today's date is {current_date}.
            Read the following school communication. Extract ALL events, including academic dates, special activities, and passive items like food drives or costume days that require preparation.

            CRITICAL DATE EXTRACTION RULES:
            1. Extract the EXACT calendar date explicitly stated next to the event in the text (e.g., if the text says "Friday, November 6", you MUST extract 2026-11-06).
            2. DO NOT calculate dates based on past weeks, DO NOT carry over dates from previous emails, and DO NOT guess. 
            3. If an event specifies a day and date (e.g., "November 6"), use that exact date.

            Extract any URLs or hyperlinks mentioned for RSVPs or extra details into 'rsvp_link' or 'info_link'.
            - If there is a schedule table breaking down times by grade or class, parse each row into the `sub_events` list, capturing its specific `target_grade` (e.g., "Grade 1", "Grade III") and start/end times.
            You are an expert at parsing school newsletters. For each event or sub-event, determine if it is an active blocker (requires parent attendance, meetings, conferences, volunteering) or a passive event (candy drives, box tops collection, book drop-offs, general spirit weeks).
            - Set `is_blocker = true` ONLY if it requires a parent's presence or a hard time commitment.
            - Set `is_blocker = false` for passive, drop-off, or ongoing collection drives where parents do not need to clear their schedule.

            Output a valid JSON object matching this exact structure:
            {{
            "events": [
                {{
                "title": "string",
                "date": "YYYY-MM-DD",
                "category": "string",
                "description": "string",
                "requires_prep": true,
                "is_blocker": true,
                "prep_notes": "string",
                "rsvp_link": "https://... or null",
                "info_link": "https://... or null"
                }}
            ]
            }}            
            """

            for attempt in range(max_retries):
                try:
                    response = client.models.generate_content(
                        model='gemini-3.6-flash',
                        contents=[prompt, clean_text[:4000]],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=NewsletterExtraction,
                            temperature=0.1,
                        ),
                    )
                    break
                except Exception as e:
                    if ("503" in str(e) or "UNAVAILABLE" in str(e)) and attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        continue
                    raise e
            if response and response.text:
                parsed = NewsletterExtraction.model_validate_json(response.text)
                print(f"[DEBUG] Successfully extracted {len(parsed.events)} event(s).")
                all_extracted_events.extend(parsed.events)
        
    return all_extracted_events



# --- Main App Execution ---
if __name__ == "__main__":
    st.set_page_config(page_title="Family School Event Review", page_icon="🏫", layout="wide")
    st.title("📚 School Newsletter Dashboard")
    st.markdown("Review unread school newsletters, check for work calendar conflicts, and manage required preparations in one place.")
    st.divider()

    # --- 1. Date Range Filter Controls ---
    st.subheader("📅 Email Date Window")
    default_start = date.today() - timedelta(days=3)
    default_end = date.today()

    date_range = st.date_input(
        "Select range to fetch (includes both read and unread messages):",
        value=(default_start, default_end),
        max_value=date.today(),
    )

    if "events" not in st.session_state:
        st.session_state.events = []

    # 1. Fetch Button (Uses local cache to prevent quota errors)
    if st.button("📥 Fetch & Parse School Emails", type="primary"):
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start_date, end_date = date_range
            with st.spinner("Connecting to Gmail and Gemini..."):
                try:
                    gmail, calendar = get_google_services()
                    st.session_state.events = get_or_fetch_events(gmail, calendar,start_date, end_date)
                    st.success(f"Successfully loaded {len(st.session_state.events)} events!")
                except Exception as e:
                    st.error(f"Error processing emails: {e}")
        else:
            st.warning("Please select a complete start and end date.")

    # 2. Render Dashboard UI
    if st.session_state.events:
        st.markdown("---")
        st.subheader("📋 Extracted Events & Conflict Review")
        
        # _, calendar = get_google_services()
        
        for idx, ev in enumerate(st.session_state.events):

            with st.container():
                cols = st.columns([3, 1])
                with cols[0]:
                    st.markdown(f"### {ev['title']}")
                    st.write(f"**Category:** {ev['category'].upper()} | **Time:** {ev['start']} to {ev['end']}")
                    # st.json(ev)
                    gemini_is_blocker = ev.get('is_blocker', True)

                    # 2. Add the Human-in-the-Loop toggle (pre-filled by Gemini)
                    is_calendar_blocker = st.checkbox(
                        "🚨 Block Calendar Time (Marks as Busy)", 
                        value=gemini_is_blocker, 
                        key=f"blocker_toggle_{idx}",
                        help="Checked = Busy (blocks calendar). Unchecked = Free (passive reminder)."
                    )
                    
                    if ev['prep'] and ev['prep'].lower() != 'none':
                        st.warning(f"🛒 **Preparation Needed:** {ev['prep']}")
                    
                    st.write(f"**Details:** {ev['description']}")
                    
                    if ev.get('sub_events'):
                        config = load_app_config()
                        
                        # Flatten all child aliases into a single searchable list
                        active_aliases = []
                        for child in config.get("children", []):
                            active_aliases.extend([alias.lower() for alias in child.get("aliases", [])])

                        with st.expander(f"📍 View Class Schedule Breakdown ({len(ev['sub_events'])} slots)"):
                            for sub in ev['sub_events']:
                                sub_grade = (sub.get('target_grade') or "").lower()
                                
                                # Dynamic match against config aliases
                                is_match = any(alias in sub_grade for alias in active_aliases)
                                
                                prefix = "⭐ **[CHILD'S CLASS MATCH]** " if is_match else "🔹 "
                                st.markdown(f"{prefix} **{sub.get('title')}**")
                                st.caption(f"⏰ {sub.get('start_datetime', 'N/A')} | Target: {sub.get('target_grade', 'All Grades')}")
                                if sub.get('description'):
                                    st.write(sub.get('description'))
                                st.divider()

                    # --- Render Hyperlinks ---
                    if ev.get('rsvp_link') or ev.get('info_link'):
                        st.markdown(
                            """
                            <style>
                            /* Changes the background color of all link buttons globally */
                            [data-testid="stLinkButton"] > a {
                                background-color: #0284c7 !important; /* Change this hex code to any color you want */
                                color: white !important;               /* Keeps text white and readable */
                                border-radius: 6px !important;
                            }
                            
                            /* Changes color smoothly when hovering over the button */
                            [data-testid="stLinkButton"] > a:hover {
                                background-color: #0369a1 !important;
                                color: white !important;
                            }
                            </style>
                            """,
                            unsafe_allow_html=True
                        )
                        link_col1, link_col2 = st.columns(2)
                        with link_col1:
                            if ev.get('rsvp_link'):
                                st.link_button("✉️ RSVP Online", ev['rsvp_link'], use_container_width=True)
                        with link_col2:
                            if ev.get('info_link'):
                                st.link_button("🔗 Read More / Info", ev['info_link'], use_container_width=True)
                        
                    # --- Conflict Status ---
                    if ev['conflicts']:
                        st.error(f"⚠️ **Schedule Conflict Detected:** Overlaps with: {', '.join(ev['conflicts'])}")
                    else:
                        st.success("✅ No calendar conflicts detected.")
                        
                with cols[1]:
                    if st.button("Add to Calendar", key=f"add_{idx}"):
                        _, calendar = get_google_services()
                        calendar_transparency = 'opaque' if is_calendar_blocker else 'transparent'
                        event_body = {
                            'summary': ev['title'],
                            'description': f"Prep: {ev['prep']}\nDetails: {ev['description']}",
                            'start': {'dateTime': ev['start'], 'timeZone': 'America/New_York'},
                            'end': {'dateTime': ev['end'], 'timeZone': 'America/New_York'},
                            'transparency': calendar_transparency,
                        }
                        try:
                            calendar.events().insert(calendarId='primary', body=event_body).execute()
                            status_label = "Busy Event" if is_calendar_blocker else "Free/Passive Reminder"
                            st.success(f"Added to Calendar as **{status_label}**!")
                        except Exception as cal_err:
                            st.error(f"Failed: {cal_err}")
                            st.markdown("---")