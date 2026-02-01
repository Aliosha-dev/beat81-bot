import requests
import datetime
import pytz
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# === API CONFIGURATION ===
BOOKING_API_URL = "https://api.gritspot.com/api/tickets"
EVENTS_API_URL = "https://api.gritspot.com/api/events"

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
                return event["id"], location_name, berlin_time.strftime('%H:%M')

        except Exception as e:
            continue

    return None, None, None


def book_event(event_id, user_id, headers):
    """Book an event for the specified user."""
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


def run_booking_for_user(user):
    """Run the booking process for a single user."""
    user_name = user.get('name', 'Unknown')
    bearer_token = user.get('bearer_token')
    user_id = user.get('user_id')
    bookings_config = user.get('bookings', [])

    print(f"\n{'='*60}")
    print(f"👤 Processing user: {user_name}")
    print(f"{'='*60}")

    if not bearer_token or not user_id:
        print(f"❌ Missing bearer_token or user_id for {user_name}")
        return []

    headers = get_headers(bearer_token)
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
            event_id, location_name, event_time = find_target_event(
                events, location, class_type, target_hour, target_minute
            )

            if event_id:
                try:
                    book_event(event_id, user_id, headers)
                    print(f"   ✅ Booked: {location_name} at {event_time}")
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


if __name__ == "__main__":
    run_booking_process()
