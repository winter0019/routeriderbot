import os
import psycopg2
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ==========================
# DATABASE CONNECTION
# ==========================

def get_db():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()

        # Drivers
        cur.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            id SERIAL PRIMARY KEY,
            phone VARCHAR(20) UNIQUE NOT NULL,
            details TEXT NOT NULL
        );
        """)

        # Trips
        cur.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id SERIAL PRIMARY KEY,
            driver_phone VARCHAR(20),
            details TEXT,
            seats INTEGER DEFAULT 0,
            price INTEGER DEFAULT 0,
            completed BOOLEAN DEFAULT FALSE
        );
        """)

        # Passengers
        cur.execute("""
        CREATE TABLE IF NOT EXISTS passengers (
            id SERIAL PRIMARY KEY,
            phone VARCHAR(20) UNIQUE NOT NULL
        );
        """)

        # Ride Requests
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ride_requests (
            id SERIAL PRIMARY KEY,
            passenger_phone VARCHAR(20),
            details TEXT,
            matched BOOLEAN DEFAULT FALSE
        );
        """)

        # Earnings table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS earnings (
            id SERIAL PRIMARY KEY,
            driver_phone VARCHAR(20),
            trip_id INTEGER,
            passenger_phone VARCHAR(20),
            amount INTEGER
        );
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized successfully")

    except Exception as e:
        print("❌ Database initialization failed:", e)


# Initialize DB on startup
init_db()

user_states = {}

# ==========================
# WEBHOOK
# ==========================

@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    elif request.method == "POST":
        data = request.get_json()
        try:
            entry = data["entry"][0]
            changes = entry["changes"][0]
            value = changes["value"]
            if "messages" in value:
                message = value["messages"][0]
                from_number = message["from"]
                message_text = message["text"]["body"]
                response = process_message(message_text, from_number)
                if response:
                    send_message(from_number, response)
        except Exception as e:
            print("❌ Webhook processing error:", e)
        return jsonify({"status": "ok"}), 200


# ==========================
# BOT LOGIC
# ==========================

def process_message(text, phone):
    text = text.strip()
    lower = text.lower()

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        # ----------------------
        # STATES
        # ----------------------
        if user_states.get(phone) == "registering":
            cur.execute(
                "INSERT INTO drivers (phone, details) VALUES (%s, %s)",
                (phone, text)
            )
            conn.commit()
            user_states.pop(phone)
            return "🎉 Registration successful! You are now a registered driver."

        # POST TRIP - Robust parsing
        if user_states.get(phone) == "posting_trip":
            try:
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                trip_data = {}
                for line in lines:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        trip_data[key.strip().upper()] = value.strip()

                required = ["DATE", "TIME", "SEATS", "PRICE"]
                missing = [field for field in required if field not in trip_data]
                if missing:
                    return f"❌ Trip format error. Missing: {', '.join(missing)}"

                # Convert seats and price
                try:
                    seats = int(trip_data["SEATS"])
                    price = int(trip_data["PRICE"])
                except ValueError:
                    return "❌ SEATS and PRICE must be numbers."

                details = f"DATE:{trip_data['DATE']} TIME:{trip_data['TIME']}"
                cur.execute(
                    "INSERT INTO trips (driver_phone, details, seats, price, completed) VALUES (%s, %s, %s, %s, FALSE)",
                    (phone, details, seats, price)
                )
                conn.commit()
                user_states.pop(phone)
                return f"🚗 Trip posted successfully! Seats: {seats}, Price: {price}"

            except Exception as e:
                print("❌ Trip parsing error:", e)
                return "❌ Trip format error. Use DATE, TIME, SEATS, PRICE."

        # REQUEST RIDE - Auto matching
        if user_states.get(phone) == "requesting_ride":
            try:
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                ride_data = {}
                for line in lines:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        ride_data[key.strip().upper()] = value.strip()

                required = ["FROM", "TO", "DATE", "TIME"]
                missing = [field for field in required if field not in ride_data]
                if missing:
                    return f"❌ Ride format error. Missing: {', '.join(missing)}"

                details = f"FROM:{ride_data['FROM']} TO:{ride_data['TO']} DATE:{ride_data['DATE']} TIME:{ride_data['TIME']}"
                cur.execute(
                    "INSERT INTO ride_requests (passenger_phone, details) VALUES (%s, %s) RETURNING id",
                    (phone, details)
                )
                ride_id = cur.fetchone()[0]
                conn.commit()
                user_states.pop(phone)

                # Auto-match available driver with open seats
                cur.execute("""
                    SELECT id, driver_phone, details, seats, price
                    FROM trips
                    WHERE completed=FALSE AND seats>0
                    ORDER BY id ASC
                    LIMIT 1
                """)
                trip = cur.fetchone()
                if trip:
                    trip_id, driver_phone, trip_details, seats, price = trip
                    # Update seats
                    cur.execute("UPDATE trips SET seats=seats-1 WHERE id=%s", (trip_id,))
                    # Mark ride request as matched
                    cur.execute("UPDATE ride_requests SET matched=TRUE WHERE id=%s", (ride_id,))
                    # Add earnings
                    cur.execute("INSERT INTO earnings (driver_phone, trip_id, passenger_phone, amount) VALUES (%s, %s, %s, %s)",
                                (driver_phone, trip_id, phone, price))
                    conn.commit()
                    # Notify driver
                    send_message(driver_phone, f"🚕 New passenger matched!\nPassenger: {phone}\nTrip: {trip_details}\nFare: {price}")
                    return f"🚕 Ride request submitted! You have been matched with a driver. Fare: {price}"
                else:
                    return "🚕 Ride request submitted! Waiting for available drivers."

            except Exception as e:
                print("❌ Ride request error:", e)
                return "❌ Ride format error. Use FROM, TO, DATE, TIME."

        # ----------------------
        # COMMANDS
        # ----------------------
        if lower == "/help":
            return """🚗 ROUTERIDER BOT COMMANDS

/register - Register as driver
/post_trip - Post a new trip
/ride - Request a ride
/my_stats - View your statistics
/complete [trip_id] - Mark trip as complete"""

        if lower == "/register":
            user_states[phone] = "registering"
            return """✅ DRIVER REGISTRATION

Reply with:
NAME:
ROUTE:
CAR:
PLATE:"""

        if lower == "/post_trip":
            cur.execute("SELECT 1 FROM drivers WHERE phone=%s", (phone,))
            if not cur.fetchone():
                return "❌ You must register first using /register"
            user_states[phone] = "posting_trip"
            return """🚗 POST TRIP

Reply with:
DATE:
TIME:
SEATS:
PRICE:"""

        if lower == "/ride":
            user_states[phone] = "requesting_ride"
            return """🧍 REQUEST A RIDE

Reply with:
FROM:
TO:
DATE:
TIME:"""

        if lower == "/my_stats":
            cur.execute("SELECT COUNT(*) FROM trips WHERE driver_phone=%s", (phone,))
            total_trips = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(amount),0) FROM earnings WHERE driver_phone=%s", (phone,))
            total_earnings = cur.fetchone()[0]
            return f"""📊 YOUR STATS

Total Trips: {total_trips}
Total Earnings: ₦{total_earnings}"""

        if lower.startswith("/complete"):
            parts = lower.split()
            if len(parts) != 2:
                return "Usage: /complete 1"
            trip_id = int(parts[1])
            cur.execute("UPDATE trips SET completed=TRUE WHERE id=%s AND driver_phone=%s", (trip_id, phone))
            conn.commit()
            if cur.rowcount == 0:
                return "❌ Trip not found."
            return "✅ Trip marked as complete!"

        return "Send /help to see available commands."

    except Exception as e:
        print("❌ Bot error:", e)
        return "⚠️ Something went wrong. Try again."

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ==========================
# SEND MESSAGE
# ==========================

def send_message(to, message):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        print("❌ WhatsApp send error:", response.text)


@app.route("/")
def home():
    return "RouteRider Bot Running 🚗", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
