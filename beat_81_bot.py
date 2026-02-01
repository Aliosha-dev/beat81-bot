import requests
import datetime
import pytz
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# === REQUIRED ENVIRONMENT VARIABLES ===
# BEARER_TOKEN - Your Beat81 API bearer token
# USER_ID - Your Beat81 user ID
# LOCATION_ID_SENDLING - Sendling location ID
# EMAIL_RECIPIENT - Email address for notifications
# EMAIL_PASSWORD - Email password for sending notifications

# === KONFIGURATION ===
BEARER_TOKEN = os.getenv('BEARER_TOKEN')
USER_ID = os.getenv('USER_ID')
LOCATION_ID_SENDLING = os.getenv('LOCATION_ID_SENDLING')
BOOKING_API_URL = "https://api.gritspot.com/api/tickets"
EVENTS_API_URL = "https://api.gritspot.com/api/events"

# Target class configuration
TARGET_TIME_HOUR = 7
TARGET_TIME_MINUTE = 35
TARGET_LOCATION_KEYWORD = "sendling"  # Will match location name containing this
TARGET_CLASS_KEYWORD = "ride"  # RIDE = spinning/cycling class
TARGET_DAYS = [0, 2, 4]  # Monday=0, Wednesday=2, Friday=4
DAYS_IN_ADVANCE = 14  # Book 2 weeks in advance

# Email configuration
EMAIL_RECIPIENT = os.getenv('EMAIL_RECIPIENT')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')
EMAIL_SUBJECT = "Beat81 Booking Status"

HEADERS = {
    "Authorization": f"Bearer {BEARER_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# === FUNKTIONEN ===

def send_email(subject, body, is_success=True):
    try:
        # Create message
        msg = MIMEMultipart()
        msg['From'] = EMAIL_RECIPIENT
        msg['To'] = EMAIL_RECIPIENT
        msg['Subject'] = subject

        # Add body
        msg.attach(MIMEText(body, 'plain'))

        # Connect to Gmail's SMTP server
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        
        # Login to your Gmail account
        server.login(EMAIL_RECIPIENT, EMAIL_PASSWORD)
        
        # Send email
        text = msg.as_string()
        server.sendmail(EMAIL_RECIPIENT, EMAIL_RECIPIENT, text)
        server.quit()
        
        print("✅ Email notification sent successfully")
    except Exception as e:
        print(f"❌ Failed to send email notification: {e}")

def get_target_dates():
    """
    Get all target dates (Mon/Wed/Fri) that are exactly 2 weeks from now.
    Returns a list of (start, end, date) tuples for each target day.
    """
    today = datetime.date.today()
    berlin = pytz.timezone("Europe/Berlin")
    target_dates = []

    # Check the next 14 days for Mon/Wed/Fri
    for days_ahead in range(DAYS_IN_ADVANCE, DAYS_IN_ADVANCE + 7):
        target_date = today + datetime.timedelta(days=days_ahead)
        if target_date.weekday() in TARGET_DAYS:  # Mon=0, Wed=2, Fri=4
            # Create time range around target time (6:00 to 9:00 to catch the 07:35 class)
            start = berlin.localize(datetime.datetime.combine(target_date, datetime.time(6, 0))).isoformat()
            end = berlin.localize(datetime.datetime.combine(target_date, datetime.time(9, 0))).isoformat()
            target_dates.append((start, end, target_date))

    return target_dates

def fetch_events(start, end):
    # Using the same params structure as the mobile app API calls
    params = {
        "$sort[date_begin]": 1,
        "date_begin_gte": start,
        "date_begin_lte": end,
        "status_ne[]": ["completed", "cancelled"],
        "is_published": "true",
        "hide_created_by_mistake": "true",
        "include[]": ["withLocations", "withEventTypes"],
        "$select[]": ["id", "special", "date_begin", "special_event_name", "special_event_description",
                      "duration", "max_participants", "coach_id", "current_participants_count",
                      "participants_count", "language", "location_id"],
        "location_city_code": "munich",
        "$skip": 0,
        "$limit": 50
    }

    print("\n=== API Request ===")
    print(f"URL: {EVENTS_API_URL}")
    print(f"Searching for events: {start.split('T')[0]}")

    try:
        response = requests.get(EVENTS_API_URL, headers=HEADERS, params=params)
        response.raise_for_status()
        data = response.json()

        print("\n=== API Response Summary ===")
        events = data.get('data', [])
        print(f"Total events found: {len(events)}")
        print("\n=== Event Details ===")
        for event in events:
            location_name = "Unknown"
            event_type = "Unknown"
            if 'location' in event:
                location_name = event['location'].get('name', 'Unknown')
            if 'event_type' in event:
                event_type = event['event_type'].get('name', 'Unknown')
            print(f"ID: {event.get('id')}")
            print(f"Location: {location_name} (ID: {event.get('location_id', 'MISSING')})")
            print(f"Type: {event_type}")
            print(f"Time: {event.get('date_begin')}")
            print(f"Participants: {event.get('current_participants_count', 0)}/{event.get('max_participants', '?')}")
            print("---")

        return events
    except requests.exceptions.HTTPError as e:
        print(f"\n❌ HTTP Error: {e}")
        print(f"Response: {e.response.text if hasattr(e, 'response') else 'No response text'}")
        return []
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return []

def find_target_event(events):
    print(f"\n=== Target Event Search ===")
    print(f"Looking for: {TARGET_LOCATION_KEYWORD.upper()} {TARGET_CLASS_KEYWORD.upper()}")
    print(f"Target time: {TARGET_TIME_HOUR:02d}:{TARGET_TIME_MINUTE:02d}")

    for event in events:
        try:
            # Parse the UTC time and convert to Berlin time
            utc_time = datetime.datetime.fromisoformat(event["date_begin"].replace("Z", "+00:00"))
            berlin_time = utc_time.astimezone(pytz.timezone("Europe/Berlin"))

            # Get location name (includes class type, e.g., "Sendling RIDE")
            location_name = "Unknown"
            if 'location' in event:
                location_name = event['location'].get('name', 'Unknown')

            print(f"\nChecking event:")
            print(f"ID: {event.get('id')}")
            print(f"Location: {location_name}")
            print(f"Berlin Time: {berlin_time.strftime('%H:%M')}")
            print(f"Participants: {event.get('current_participants_count', 0)}/{event.get('max_participants', '?')}")

            # Check if location name contains both location keyword and class keyword
            # e.g., "Sendling RIDE" contains "sendling" and "ride"
            location_name_lower = location_name.lower()
            location_matches = TARGET_LOCATION_KEYWORD.lower() in location_name_lower
            class_matches = TARGET_CLASS_KEYWORD.lower() in location_name_lower

            # Check time match
            time_matches = (berlin_time.hour == TARGET_TIME_HOUR and
                          berlin_time.minute == TARGET_TIME_MINUTE)

            if location_matches and class_matches and time_matches:
                print(f"✅ Found matching class: {location_name} at {berlin_time.strftime('%H:%M')}!")
                return event["id"]

        except Exception as e:
            print(f"Error processing event: {e}")
            continue

    print("❌ No matching event found")
    return None

def book_event(event_id):
    payload = {
        "user_id": USER_ID,
        "event_id": event_id
    }

    print("\n=== Booking Request ===")
    print(f"Event ID: {event_id}")
    print(f"Payload: {payload}")
    
    try:
        response = requests.post(BOOKING_API_URL, headers=HEADERS, json=payload)
        response.raise_for_status()
        booking_data = response.json()
        print("\n=== Booking Response ===")
        print(f"Booking successful! Response: {booking_data}")
        return booking_data
    except requests.exceptions.HTTPError as e:
        print(f"\n❌ Booking Error: {e}")
        print(f"Response: {e.response.text if hasattr(e, 'response') else 'No response text'}")
        raise
    except Exception as e:
        print(f"\n❌ Error during booking: {e}")
        raise

def run_booking_process():
    print(f"\n=== Starting booking process at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Target: Spinning at Sendling, {TARGET_TIME_HOUR:02d}:{TARGET_TIME_MINUTE:02d}")
    print(f"Days: Monday, Wednesday, Friday")
    print(f"Booking {DAYS_IN_ADVANCE} days in advance")

    target_dates = get_target_dates()

    if not target_dates:
        print("❌ No target dates found in the next 2 weeks")
        return

    bookings_made = []
    bookings_failed = []

    for start, end, target_date in target_dates:
        day_name = target_date.strftime('%A')
        print(f"\n{'='*50}")
        print(f"Processing {day_name}, {target_date}")
        print(f"{'='*50}")

        try:
            events = fetch_events(start, end)
            event_id = find_target_event(events)

            if event_id:
                booking = book_event(event_id)
                success_info = {
                    'date': target_date,
                    'day': day_name,
                    'event_id': event_id
                }
                bookings_made.append(success_info)
                print(f"✅ Successfully booked {day_name}, {target_date}!")
            else:
                bookings_failed.append({
                    'date': target_date,
                    'day': day_name,
                    'reason': 'No matching event found'
                })
                print(f"❌ No matching event found for {day_name}, {target_date}")

        except Exception as e:
            bookings_failed.append({
                'date': target_date,
                'day': day_name,
                'reason': str(e)
            })
            print(f"❌ Error booking {day_name}, {target_date}: {e}")

    # Send summary email
    summary = f"Beat81 Booking Summary\n\n"
    summary += f"Target: Spinning at Sendling, {TARGET_TIME_HOUR:02d}:{TARGET_TIME_MINUTE:02d}\n\n"

    if bookings_made:
        summary += "✅ Successfully Booked:\n"
        for b in bookings_made:
            summary += f"  - {b['day']}, {b['date']}\n"

    if bookings_failed:
        summary += "\n❌ Failed to Book:\n"
        for b in bookings_failed:
            summary += f"  - {b['day']}, {b['date']}: {b['reason']}\n"

    print(f"\n{'='*50}")
    print(summary)

    is_success = len(bookings_made) > 0
    subject = "Beat81 Booking " + ("Success" if is_success else "Failed")
    send_email(subject, summary, is_success=is_success)


if __name__ == "__main__":
    run_booking_process()