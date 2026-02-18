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
        raise Exception("DATABASE_URL not set in Railway variables")
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
            seats INTEGER DEFAULT 4,
            price FLOAT DEFAULT 0,
            completed BOOLEAN DEFAULT FALSE
        );
        """)

        # Ride Requests
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ride_requests (
            id SERIAL PRIMARY KEY,
            passenger_phone VARCHAR(20),
            from_location TEXT,
            to_location TEXT,
            trip_date TEXT,
            trip_time TEXT,
            matched BOOLEAN DEFAULT FALSE,
            driver_phone VARCHAR(20)
        );
        """)

        # Earnings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS earnings (
            id SERIAL PRIMARY KEY,
            driver_phone VARCHAR(20) UNIQUE,
            total FLOAT DEFAULT 0
        );
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized successfully")

    except Exception as e:
        print("❌ Database initialization failed:", e)

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

    if request.method == "POST":
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
        # Driver Registration
        # ----------------------
        if user_states.get(phone) == "registering":
            cur.execute(
                "INSERT INTO drivers (phone, details) VALUES (%s, %s)",
                (phone, text),
            )
            conn.commit()
            user_states.pop(phone)
            return "🎉 Registration successful! You are now a registered driver."

        # ----------------------
        # Trip Posting
        # ----------------------
        if user_states.get(phone) == "posting_trip":
            try:
                lines = text.split("\n")
                details = []
                seats = 4
                price = 0
                for line in lines:
                    if "seats" in line.lower():
                        seats = int(line.split(":")[1].strip())
                    elif "price" in line.lower():
                        price = float(line.split(":")[1].strip())
                    else:
                        details.append(line.strip())
                cur.execute(
                    "INSERT INTO trips (driver_phone, details, seats, price) VALUES (%s, %s, %s, %s)",
                    (phone, "\n".join(details), seats, price),
                )
                conn.commit()
                user_states.pop(phone)
                return f"🚗 Trip posted successfully with {seats} seats at ${price}!"
            except Exception:
                return "❌ Trip format error. Use DATE, TIME, SEATS, PRICE."

        # ----------------------
        # Ride Request
        # ----------------------
        if user_states.get(phone) == "requesting_ride":
            try:
                lines = text.split("\n")
                data = {}
                for line in lines:
                    key, value = line.split(":", 1)
                    data[key.strip().lower()] = value.strip()
                from_loc = data["from"]
                to_loc = data["to"]
                trip_date = data["date"]
                trip_time = data["time"]
            except Exception:
                return "❌ Invalid format. Use FROM: ... TO: ... DATE: ... TIME: ..."

            cur.execute(
                "INSERT INTO ride_requests (passenger_phone, from_location, to_location, trip_date, trip_time) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (phone, from_loc, to_loc, trip_date, trip_time),
            )
            ride_id = cur.fetchone()[0]

            # ----------------------
            # Auto-match driver
            # ----------------------
            cur.execute(
                "SELECT id, driver_phone, seats, price FROM trips WHERE details ILIKE %s AND completed=FALSE AND seats>0 ORDER BY id LIMIT 1",
                (f"%{from_loc}%{to_loc}%",),
            )
            trip = cur.fetchone()
            if trip:
                trip_id, driver_phone, seats, price = trip
                # Update ride request
                cur.execute(
                    "UPDATE ride_requests SET matched=TRUE, driver_phone=%s WHERE id=%s",
                    (driver_phone, ride_id),
                )
                # Reduce seats
                cur.execute("UPDATE trips SET seats=seats-1 WHERE id=%s", (trip_id,))
                # Update earnings
                cur.execute(
                    "INSERT INTO earnings (driver_phone, total) VALUES (%s, %s) "
                    "ON CONFLICT (driver_phone) DO UPDATE SET total=earnings.total + %s",
                    (driver_phone, price, price),
                )
                conn.commit()
                user_states.pop(phone)
                send_message(driver_phone, f"🚕 New ride request from {phone}: {from_loc} → {to_loc} on {trip_date} at {trip_time}.")
                return f"✅ Ride matched with driver {driver_phone}! Price: ${price}"
            conn.commit()
            user_states.pop(phone)
            return "🚕 Ride request submitted! Waiting for available drivers."

        # ----------------------
        # Commands
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
            return "✅ DRIVER REGISTRATION\nReply with:\nNAME:\nROUTE:\nCAR:\nPLATE:"

        if lower == "/post_trip":
            cur.execute("SELECT 1 FROM drivers WHERE phone=%s", (phone,))
            if not cur.fetchone():
                return "❌ You must register first using /register"
            user_states[phone] = "posting_trip"
            return "🚗 POST TRIP\nReply with:\nDATE:\nTIME:\nSEATS:\nPRICE:"

        if lower == "/ride":
            user_states[phone] = "requesting_ride"
            return "🧍 REQUEST A RIDE\nReply with:\nFROM:\nTO:\nDATE:\nTIME:"

        if lower == "/my_stats":
            cur.execute("SELECT COUNT(*) FROM trips WHERE driver_phone=%s", (phone,))
            total_trips = cur.fetchone()[0]
            cur.execute("SELECT total FROM earnings WHERE driver_phone=%s", (phone,))
            earnings = cur.fetchone()
            earnings = earnings[0] if earnings else 0
            return f"📊 YOUR STATS\nTotal Trips: {total_trips}\nTotal Earnings: ${earnings}"

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
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        print("❌ WhatsApp send error:", response.text)


@app.route("/")
def home():
    return "RouteRider Bot Running 🚗", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
