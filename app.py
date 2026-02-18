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

        # Drivers table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            id SERIAL PRIMARY KEY,
            phone VARCHAR(20) UNIQUE NOT NULL,
            details TEXT NOT NULL
        );
        """)

        # Trips table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id SERIAL PRIMARY KEY,
            driver_phone VARCHAR(20),
            details TEXT,
            from_location VARCHAR(50),
            to_location VARCHAR(50),
            seats INT DEFAULT 0,
            price NUMERIC DEFAULT 0,
            completed BOOLEAN DEFAULT FALSE
        );
        """)

        # Passengers table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS passengers (
            id SERIAL PRIMARY KEY,
            phone VARCHAR(20) UNIQUE NOT NULL
        );
        """)

        # Ride requests table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ride_requests (
            id SERIAL PRIMARY KEY,
            passenger_phone VARCHAR(20),
            details TEXT,
            from_location VARCHAR(50),
            to_location VARCHAR(50),
            matched BOOLEAN DEFAULT FALSE
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

        # --------------------------
        # STATE HANDLING
        # --------------------------
        if user_states.get(phone) == "registering":
            cur.execute(
                "INSERT INTO drivers (phone, details) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (phone, text)
            )
            conn.commit()
            user_states.pop(phone)
            return "🎉 Registration successful! You are now a registered driver."

        if user_states.get(phone) == "posting_trip":
            # Parse the trip details
            try:
                details = {}
                for line in text.split("\n"):
                    key, val = line.split(":", 1)
                    details[key.strip().lower()] = val.strip()

                # Validate numeric values
                seats = int(details.get("seats", 0))
                price = float(details.get("price", 0))
                from_loc = details.get("from")
                to_loc = details.get("to")
                if not all([from_loc, to_loc, seats, price]):
                    raise ValueError("Missing required fields")

                cur.execute("""
                    INSERT INTO trips (driver_phone, details, from_location, to_location, seats, price)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (phone, text, from_loc, to_loc, seats, price))
                conn.commit()
                user_states.pop(phone)
                return "🚗 Trip posted successfully!"
            except Exception as e:
                print("❌ Post trip error:", e)
                return "❌ Trip format error. Example:\nFROM: Kano\nTO: Katsina\nDATE: 19/02/2026\nTIME: 4:00 pm\nSEATS: 2\nPRICE: 3500"

        if user_states.get(phone) == "requesting_ride":
            try:
                details = {}
                for line in text.split("\n"):
                    key, val = line.split(":", 1)
                    details[key.strip().lower()] = val.strip()

                from_loc = details.get("from")
                to_loc = details.get("to")
                if not all([from_loc, to_loc]):
                    raise ValueError("Missing FROM or TO")

                # Insert ride request
                cur.execute("""
                    INSERT INTO ride_requests (passenger_phone, details, from_location, to_location)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """, (phone, text, from_loc, to_loc))
                ride_id = cur.fetchone()[0]
                conn.commit()
                user_states.pop(phone)

                # Automatic matching
                cur.execute("""
                    SELECT id, driver_phone, seats FROM trips
                    WHERE from_location=%s AND to_location=%s AND completed=FALSE AND seats > 0
                    ORDER BY id ASC LIMIT 1
                """, (from_loc, to_loc))
                trip = cur.fetchone()
                if trip:
                    trip_id, driver_phone, seats = trip
                    # Reduce seat
                    cur.execute("UPDATE trips SET seats = seats - 1 WHERE id=%s", (trip_id,))
                    cur.execute("UPDATE ride_requests SET matched=TRUE WHERE id=%s", (ride_id,))
                    conn.commit()
                    # Notify driver
                    send_message(driver_phone, f"🚕 New passenger matched: {phone}, FROM: {from_loc}, TO: {to_loc}")
                    return "🚕 Ride request submitted and matched to a driver!"
                else:
                    return "🚕 Ride request submitted! Drivers will be matched soon."

            except Exception as e:
                print("❌ Ride request error:", e)
                return "❌ Ride format error. Example:\nFROM: Kano\nTO: Katsina\nDATE: 19/02/2026\nTIME: 4:00 pm"

        # --------------------------
        # COMMANDS
        # --------------------------
        if lower == "/help":
            return """🚗 ROUTERIDER BOT COMMANDS

/register - Register as driver
/post_trip - Post a new trip
/ride - Request a ride
/my_stats - View your statistics
/complete [trip_id] - Mark trip as complete"""

        if lower == "/register":
            user_states[phone] = "registering"
            return "✅ DRIVER REGISTRATION\nReply with:\nNAME:\nROUTE:\nCAR:\nPLATE:"

        if lower == "/post_trip":
            cur.execute("SELECT 1 FROM drivers WHERE phone=%s", (phone,))
            if not cur.fetchone():
                return "❌ You must register first using /register"
            user_states[phone] = "posting_trip"
            return "🚗 POST TRIP\nReply with:\nFROM:\nTO:\nDATE:\nTIME:\nSEATS:\nPRICE:"

        if lower == "/ride":
            user_states[phone] = "requesting_ride"
            return "🧍 REQUEST A RIDE\nReply with:\nFROM:\nTO:\nDATE:\nTIME:"

        if lower == "/my_stats":
            cur.execute("SELECT COUNT(*) FROM trips WHERE driver_phone=%s", (phone,))
            total_trips = cur.fetchone()[0]
            return f"📊 YOUR STATS\nTotal Trips: {total_trips}\nMore analytics coming soon!"

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
        if cur: cur.close()
        if conn: conn.close()


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
