import argparse
import requests
import datetime
import pytz
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# === API CONFIGURATION ===
API_BASE = "https://api.production.b81.io/api"
BOOKING_API_URL = f"{API_BASE}/tickets"
EVENTS_API_URL = f"{API_BASE}/events"
TICKETS_API_URL = f"{API_BASE}/tickets"

DRY_RUN = False

# === USERS CONFIGURATION ===
# Load users from JSON file or environment variable
def load_users():
    """
    Load user configurations from users.json file or USERS_CONFIG env variable.
    Each user has their own bearer token, user_id, and booking preferences.
    """
    # Try to load from environment variable first (for GitHub Actions)
    users_config = os.getenv('USERS_CONFIG')
    if users_config:
        try:
            return json.loads(users_config)
        except json.JSONDecodeError as e:
            print(f"❌ Error parsing USERS_CONFIG: {e}")
            return []

    # Try to load from users.json file
    config_path = os.path.join(os.path.dirname(__file__), 'users.json')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ Error loading users.json: {e}")
            return []

    # Fallback to single user from environment variables (backwards compatibility)
    bearer_token = os.getenv('BEARER_TOKEN')
    user_id = os.getenv('USER_ID')
    if bearer_token and user_id:
        return [{
            "name": "Default User",
            "bearer_token": bearer_token,
            "user_id": user_id,
            "bookings": [{
                "location": "sendling",
                "class_type": "ride",
                "time": "07:35",
                "days": ["monday", "wednesday", "friday"],
                "days_in_advance": 14
            }]
        }]

    return []


def get_headers(bearer_token):
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def parse_time(time_str):
    """Parse time string like '07:35' into hour and minute."""
    parts = time_str.split(':')
    return int(parts[0]), int(parts[1])


def parse_days(days_list):
    """Convert day names to weekday numbers (Monday=0, Sunday=6)."""
    day_map = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6,
        'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6
    }
    return [day_map[d.lower()] for d in days_list if d.lower() in day_map]


def get_target_dates(target_days, days_in_advance, target_hour, target_minute):
    """
    Get all target dates for the specified days that are the specified days in advance.
    Returns a list of (start, end, date) tuples for each target day.
    """
    today = datetime.date.today()
    berlin = pytz.timezone("Europe/Berlin")
    target_dates = []

    # Check days from days_in_advance to days_in_advance + 7
    for days_ahead in range(days_in_advance, days_in_advance + 7):
        target_date = today + datetime.timedelta(days=days_ahead)
        if target_date.weekday() in target_days:
            # Create time range around target time (2 hours before to 2 hours after)
            start_hour = max(0, target_hour - 2)
            end_hour = min(23, target_hour + 2)
            start = berlin.localize(datetime.datetime.combine(target_date, datetime.time(start_hour, 0))).isoformat()
            end = berlin.localize(datetime.datetime.combine(target_date, datetime.time(end_hour, 59))).isoformat()
            target_dates.append((start, end, target_date))

    return target_dates


def fetch_events(start, end, headers):
    """Fetch events from the API for the given time range."""
    params = {
        "$sort[date_begin]": 1,
        "$sort[coach_id]": 1,
        "date_begin_gte": start,
        "date_begin_lte": end,
        "status_ne": "cancelled",
        "is_published": "true",
        "include[]": ["withLocations", "withEventTypes"],
        "location_city_code": "munich",
        "$skip": 0,
        "$limit": 50,
    }

    try:
        response = requests.get(EVENTS_API_URL, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('data', [])
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        return []
    except Exception as e:
        print(f"❌ Error: {e}")
        return []


def find_target_event(events, location_keyword, class_keyword, target_hour, target_minute):
    """Find an event matching the specified criteria."""
    for event in events:
        try:
            # Parse the UTC time and convert to Berlin time
            utc_time = datetime.datetime.fromisoformat(event["date_begin"].replace("Z", "+00:00"))
            berlin_time = utc_time.astimezone(pytz.timezone("Europe/Berlin"))

            # Get location name (includes class type, e.g., "Sendling RIDE")
            location_name = "Unknown"
            if 'location' in event:
                location_name = event['location'].get('name', 'Unknown')

            # Check if location name contains both location keyword and class keyword
            location_name_lower = location_name.lower()
            location_matches = location_keyword.lower() in location_name_lower
            class_matches = class_keyword.lower() in location_name_lower

            # Check time match
            time_matches = (berlin_time.hour == target_hour and
                          berlin_time.minute == target_minute)

            if location_matches and class_matches and time_matches:
                return event["id"], location_name, berlin_time.strftime('%H:%M'), event

        except Exception as e:
            continue

    return None, None, None, None


# === CALENDAR INVITE ===

SMTP_SENDER = os.getenv('EMAIL_RECIPIENT')  # Reused as the Gmail sender address
SMTP_PASSWORD = os.getenv('EMAIL_PASSWORD')  # Gmail app password


def build_ics(event, location_name, attendee_email, attendee_name):
    """Build an ICS REQUEST body for a Beat81 class."""
    utc_start = datetime.datetime.fromisoformat(event["date_begin"].replace("Z", "+00:00"))
    utc_start = utc_start.astimezone(pytz.UTC)
    duration_minutes = int(event.get("duration") or 50)
    utc_end = utc_start + datetime.timedelta(minutes=duration_minutes)

    def fmt(dt):
        return dt.strftime("%Y%m%dT%H%M%SZ")

    dtstamp = fmt(datetime.datetime.now(pytz.UTC))
    uid = f"beat81-{event['id']}@b81-bot"
    summary = f"Beat81 — {location_name}"
    description = (
        f"Auto-booked Beat81 class.\\n"
        f"Class: {location_name}\\n"
        f"Event ID: {event['id']}"
    )

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//beat81-bot//EN\r\n"
        "METHOD:REQUEST\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{dtstamp}\r\n"
        f"DTSTART:{fmt(utc_start)}\r\n"
        f"DTEND:{fmt(utc_end)}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DESCRIPTION:{description}\r\n"
        f"LOCATION:{location_name}\r\n"
        f"ORGANIZER;CN=Beat81 Bot:mailto:{SMTP_SENDER}\r\n"
        f"ATTENDEE;CN={attendee_name};RSVP=TRUE;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:{attendee_email}\r\n"
        "STATUS:CONFIRMED\r\n"
        "SEQUENCE:0\r\n"
        "TRANSP:OPAQUE\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def send_calendar_invite(event, location_name, attendee_email, attendee_name):
    """Send a calendar invite (ICS REQUEST) for a successfully booked class."""
    if not SMTP_SENDER or not SMTP_PASSWORD:
        print(f"   ⚠️ EMAIL_RECIPIENT/EMAIL_PASSWORD not set — skipping invite")
        return False
    if not attendee_email:
        print(f"   ⚠️ No invite_email configured for {attendee_name} — skipping invite")
        return False
    if DRY_RUN:
        print(f"   🧪 DRY-RUN would send invite to {attendee_email}")
        return True

    try:
        ics = build_ics(event, location_name, attendee_email, attendee_name)
        utc_start = datetime.datetime.fromisoformat(event["date_begin"].replace("Z", "+00:00"))
        berlin_start = utc_start.astimezone(pytz.timezone("Europe/Berlin"))

        msg = MIMEMultipart("mixed")
        msg["From"] = SMTP_SENDER
        msg["To"] = attendee_email
        msg["Subject"] = f"Beat81: {location_name} — {berlin_start.strftime('%a %d %b, %H:%M')}"

        body_text = (
            f"Auto-booked by beat81-bot.\n\n"
            f"Class: {location_name}\n"
            f"When: {berlin_start.strftime('%A %d %B %Y, %H:%M')} (Berlin)\n"
        )
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain"))
        alt.attach(MIMEText(ics, "calendar; method=REQUEST; charset=UTF-8"))
        msg.attach(alt)

        ics_attach = MIMEBase("text", "calendar", method="REQUEST", name="invite.ics")
        ics_attach.set_payload(ics.encode("utf-8"))
        encoders.encode_base64(ics_attach)
        ics_attach.add_header("Content-Disposition", "attachment; filename=invite.ics")
        msg.attach(ics_attach)

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SMTP_SENDER, SMTP_PASSWORD)
            server.sendmail(SMTP_SENDER, [attendee_email], msg.as_string())

        print(f"   📅 Calendar invite sent to {attendee_email}")
        return True
    except Exception as e:
        print(f"   ⚠️ Failed to send calendar invite: {e}")
        return False


def book_event(event_id, user_id, headers):
    """Book an event for the specified user."""
    if DRY_RUN:
        return {"dry_run": True, "event_id": event_id, "user_id": user_id}

    payload = {
        "user_id": user_id,
        "event_id": event_id
    }

    try:
        response = requests.post(BOOKING_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        error_text = e.response.text if hasattr(e, 'response') else 'No response text'
        raise Exception(f"Booking failed: {error_text}")


def fetch_existing_tickets(user_id, headers):
    """Fetch the user's existing (non-cancelled) future tickets. Returns a set of event_ids."""
    now_berlin = datetime.datetime.now(pytz.timezone("Europe/Berlin")).isoformat()
    params = {
        "user_id": user_id,
        "status_ne": "cancelled",
        "event_date_begin_gte": now_berlin,
        "$limit": 200,
    }
    try:
        response = requests.get(TICKETS_API_URL, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        tickets = data.get('data', data) if isinstance(data, dict) else data
        return {t.get('event_id') for t in tickets if t.get('event_id')}
    except Exception as e:
        print(f"   ⚠️ Could not fetch existing tickets: {e}")
        return set()


def run_booking_for_user(user):
    """Run the booking process for a single user."""
    user_name = user.get('name', 'Unknown')
    bearer_token = user.get('bearer_token')
    user_id = user.get('user_id')
    invite_email = user.get('invite_email')
    bookings_config = user.get('bookings', [])

    print(f"\n{'='*60}")
    print(f"👤 Processing user: {user_name}")
    print(f"{'='*60}")

    if not bearer_token or not user_id:
        print(f"❌ Missing bearer_token or user_id for {user_name}")
        return []

    headers = get_headers(bearer_token)
    existing_event_ids = fetch_existing_tickets(user_id, headers)
    if existing_event_ids:
        print(f"   ℹ️ User already has {len(existing_event_ids)} upcoming ticket(s)")
    results = []

    for booking in bookings_config:
        location = booking.get('location', 'sendling')
        class_type = booking.get('class_type', 'ride')
        time_str = booking.get('time', '07:35')
        days = booking.get('days', ['monday', 'wednesday', 'friday'])
        days_in_advance = booking.get('days_in_advance', 14)

        target_hour, target_minute = parse_time(time_str)
        target_days = parse_days(days)

        print(f"\n📋 Booking config: {location.upper()} {class_type.upper()} at {time_str}")
        print(f"   Days: {', '.join(days)}, {days_in_advance} days in advance")

        target_dates = get_target_dates(target_days, days_in_advance, target_hour, target_minute)

        for start, end, target_date in target_dates:
            day_name = target_date.strftime('%A')
            print(f"\n   📅 {day_name}, {target_date}")

            events = fetch_events(start, end, headers)
            event_id, location_name, event_time, event = find_target_event(
                events, location, class_type, target_hour, target_minute
            )

            if event_id and event_id in existing_event_ids:
                print(f"   ⏭️ Already booked: {location_name} at {event_time}")
                results.append({
                    'user': user_name,
                    'date': str(target_date),
                    'day': day_name,
                    'class': f"{location_name} at {event_time}",
                    'status': 'already_booked',
                })
                continue

            if event_id:
                try:
                    book_event(event_id, user_id, headers)
                    print(f"   {'🧪 DRY-RUN would book' if DRY_RUN else '✅ Booked'}: {location_name} at {event_time}")
                    send_calendar_invite(event, location_name, invite_email, user_name)
                    results.append({
                        'user': user_name,
                        'date': str(target_date),
                        'day': day_name,
                        'class': f"{location_name} at {event_time}",
                        'status': 'success'
                    })
                except Exception as e:
                    print(f"   ❌ Failed to book: {e}")
                    results.append({
                        'user': user_name,
                        'date': str(target_date),
                        'day': day_name,
                        'class': f"{location} {class_type} at {time_str}",
                        'status': 'failed',
                        'error': str(e)
                    })
            else:
                print(f"   ❌ No matching class found")
                results.append({
                    'user': user_name,
                    'date': str(target_date),
                    'day': day_name,
                    'class': f"{location} {class_type} at {time_str}",
                    'status': 'not_found'
                })

    return results


def run_booking_process():
    """Main booking process for all users."""
    print(f"\n{'='*60}")
    print(f"🏋️ Beat81 Auto-Booking Bot")
    print(f"⏰ Started at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    users = load_users()

    if not users:
        print("❌ No users configured. Please set up users.json or environment variables.")
        return

    print(f"\n👥 Found {len(users)} user(s) to process")

    all_results = []
    for user in users:
        results = run_booking_for_user(user)
        all_results.extend(results)

    # Print summary
    print(f"\n{'='*60}")
    print("📊 BOOKING SUMMARY")
    print(f"{'='*60}")

    successful = [r for r in all_results if r['status'] == 'success']
    failed = [r for r in all_results if r['status'] == 'failed']
    not_found = [r for r in all_results if r['status'] == 'not_found']

    if successful:
        print(f"\n✅ Successfully booked ({len(successful)}):")
        for r in successful:
            print(f"   - {r['user']}: {r['day']} {r['date']} - {r['class']}")

    if failed:
        print(f"\n❌ Failed to book ({len(failed)}):")
        for r in failed:
            print(f"   - {r['user']}: {r['day']} {r['date']} - {r.get('error', 'Unknown error')}")

    if not_found:
        print(f"\n⚠️ Class not found ({len(not_found)}):")
        for r in not_found:
            print(f"   - {r['user']}: {r['day']} {r['date']} - {r['class']}")

    print(f"\n{'='*60}")
    print(f"✅ Completed at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")


def run_test_invite():
    """Send one calendar invite per configured user using the next matching event,
    without booking anything. For verifying SMTP/ICS plumbing."""
    print("🧪 TEST-INVITE mode: sending one invite per user, no bookings\n")
    users = load_users()
    for user in users:
        user_name = user.get('name', 'Unknown')
        bearer_token = user.get('bearer_token')
        user_id = user.get('user_id')
        invite_email = user.get('invite_email')
        bookings_config = user.get('bookings', [])
        if not (bearer_token and user_id and invite_email and bookings_config):
            print(f"❌ Skipping {user_name}: missing config")
            continue
        headers = get_headers(bearer_token)
        b = bookings_config[0]
        target_hour, target_minute = parse_time(b.get('time', '07:35'))
        target_days = parse_days(b.get('days', ['monday', 'wednesday', 'friday']))
        target_dates = get_target_dates(target_days, b.get('days_in_advance', 14), target_hour, target_minute)
        sent = False
        for start, end, target_date in target_dates:
            events = fetch_events(start, end, headers)
            event_id, location_name, event_time, event = find_target_event(
                events, b.get('location', 'sendling'), b.get('class_type', 'ride'),
                target_hour, target_minute,
            )
            if event:
                print(f"👤 {user_name}: sending test invite for {location_name} on {target_date}")
                send_calendar_invite(event, location_name, invite_email, user_name)
                sent = True
                break
        if not sent:
            print(f"❌ {user_name}: no matching event found in target window")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Beat81 Auto-Booking Bot")
    parser.add_argument("--dry-run", action="store_true", help="Find classes but do not book")
    parser.add_argument("--test-invite", action="store_true",
                        help="Send one calendar invite per user without booking (smoke test)")
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    if args.test_invite:
        run_test_invite()
    else:
        run_booking_process()
