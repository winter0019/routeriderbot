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
            seats INT DEFAULT 0,
            price NUMERIC DEFAULT 0,
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

        # =====================
        # STATE HANDLING
        # =====================

        if user_states.get(phone) == "registering":
            cur.execute(
                "INSERT INTO drivers (phone, details) VALUES (%s, %s)",
                (phone, text),
            )
            conn.commit()
            user_states.pop(phone)
            return "🎉 Registration successful! You are now a registered driver."

        if user_states.get(phone) == "posting_trip":
            try:
                # Parse trip details
                lines = [line.strip() for line in text.split("\n") if line.strip()]
                trip_data = {}
                for line in lines:
                    if ":" in line:
                        key, val = line.split(":", 1)
                        trip_data[key.strip().lower()] = val.strip()

                # Validate required fields
                for field in ["date", "time", "seats", "price"]:
                    if field not in trip_data:
                        return f"❌ Missing {field.upper()} in trip details."

                seats = int(trip_data["seats"])
                price = float(trip_data["price"])

                details_str = f"DATE: {trip_data['date']}, TIME: {trip_data['time']}"

                cur.execute(
                    "INSERT INTO trips (driver_phone, details, seats, price) VALUES (%s, %s, %s, %s)",
                    (phone, details_str, seats, price)
                )
                conn.commit()
                user_states.pop(phone)
                return "🚗 Trip posted successfully!"

            except Exception as e:
                print("❌ Post trip error:", e)
                return "❌ Trip format error. Use DATE, TIME, SEATS, PRICE with numeric SEATS and PRICE."

        if user_states.get(phone) == "requesting_ride":
            try:
                # Insert the ride request
                cur.execute(
                    "INSERT INTO ride_requests (passenger_phone, details) VALUES (%s, %s) RETURNING id",
                    (phone, text)
                )
                ride_request_id = cur.fetchone()[0]

                # Parse passenger details
                lines = [line.strip() for line in text.split("\n") if line.strip()]
                ride_info = {}
                for line in lines:
                    if ":" in line:
                        key, val = line.split(":", 1)
                        ride_info[key.strip().lower()] = val.strip()

                # Find available driver
                cur.execute("""
                    SELECT id, driver_phone, seats, details, price
                    FROM trips
                    WHERE completed = FALSE AND seats > 0
                    ORDER BY id ASC
                    LIMIT 1
                """)
                driver_trip = cur.fetchone()

                if not driver_trip:
                    return "🚕 Ride request submitted! Waiting for available drivers..."

                trip_id, driver_phone, seats, trip_details, price = driver_trip

                # Reduce seat
                cur.execute(
                    "UPDATE trips SET seats = seats - 1 WHERE id = %s",
                    (trip_id,)
                )
                cur.execute(
                    "UPDATE ride_requests SET matched = TRUE WHERE id = %s",
                    (ride_request_id,)
                )
                conn.commit()

                # Notify passenger
                passenger_msg = (
                    f"✅ Ride matched!\nDriver: {driver_phone}\n"
                    f"Trip: {trip_details}\nSeats left: {seats-1}\nPrice: {price}"
                )

                # Notify driver
                driver_msg = (
                    f"🚗 New passenger booked!\nPassenger: {phone}\n"
                    f"Trip: {trip_details}\nSeats left: {seats-1}"
                )

                send_message(phone, passenger_msg)
                send_message(driver_phone, driver_msg)
                user_states.pop(phone)
                return None

            except Exception as e:
                print("❌ Ride matching error:", e)
                return "⚠️ Something went wrong. Try again."

        # =====================
        # COMMANDS
        # =====================

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
DATE: dd/mm/yyyy
TIME: hh:mm am/pm
SEATS: number
PRICE: amount"""

        if lower == "/ride":
            user_states[phone] = "requesting_ride"
            return """🧍 REQUEST A RIDE

Reply with:
FROM:
TO:
DATE:
TIME:"""

        if lower == "/my_stats":
            cur.execute(
                "SELECT COUNT(*) FROM trips WHERE driver_phone=%s",
                (phone,),
            )
            total_trips = cur.fetchone()[0]

            return f"""📊 YOUR STATS

Total Trips: {total_trips}
More analytics coming soon!"""

        if lower.startswith("/complete"):
            parts = lower.split()
            if len(parts) != 2:
                return "Usage: /complete 1"

            trip_id = int(parts[1])

            cur.execute(
                "UPDATE trips SET completed=TRUE WHERE id=%s AND driver_phone=%s",
                (trip_id, phone),
            )
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
