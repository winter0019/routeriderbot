import os
import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
from datetime import datetime

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
        raise RuntimeError("DATABASE_URL not set in Railway variables")
    # psycopg v3 with dict rows
    return psycopg.connect(
        DATABASE_URL,
        sslmode="require",
        row_factory=dict_row
    )

def init_db():
    """Safe to run on boot. Creates tables if missing."""
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id SERIAL PRIMARY KEY,
          phone VARCHAR(20) UNIQUE NOT NULL,
          role VARCHAR(12) NOT NULL CHECK (role IN ('driver','passenger')),
          full_name VARCHAR(120),
          created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS driver_profiles (
          id SERIAL PRIMARY KEY,
          user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
          route_from TEXT,
          route_to TEXT,
          car_model TEXT,
          plate TEXT,
          verified BOOLEAN DEFAULT FALSE,
          created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS trips (
          id SERIAL PRIMARY KEY,
          driver_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          origin TEXT NOT NULL,
          destination TEXT NOT NULL,
          trip_date DATE,
          trip_time TIME,
          seats_total INTEGER NOT NULL CHECK (seats_total > 0),
          seats_booked INTEGER DEFAULT 0 CHECK (seats_booked >= 0),
          price_per_seat INTEGER NOT NULL CHECK (price_per_seat >= 0),
          status TEXT DEFAULT 'active' CHECK (status IN ('active','completed','cancelled')),
          created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
          id SERIAL PRIMARY KEY,
          trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
          passenger_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          seats INTEGER DEFAULT 1 CHECK (seats > 0),
          amount_paid INTEGER DEFAULT 0 CHECK (amount_paid >= 0),
          status TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','cancelled','completed')),
          created_at TIMESTAMP DEFAULT NOW(),
          UNIQUE (trip_id, passenger_user_id)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS ride_requests (
          id SERIAL PRIMARY KEY,
          passenger_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          origin TEXT NOT NULL,
          destination TEXT NOT NULL,
          ride_date DATE,
          ride_time TIME,
          matched_trip_id INTEGER REFERENCES trips(id),
          matched BOOLEAN DEFAULT FALSE,
          created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized OK")
    except Exception as e:
        print("❌ Database init failed:", e)

# run on boot
init_db()

# ==========================
# SIMPLE STATE MACHINE
# ==========================
user_states = {}  # phone -> {"state": "register_driver" | "post_trip" | "request_ride"}

# ==========================
# HELPERS
# ==========================

def parse_kv_lines(text: str):
    """
    Accepts lines like:
      KEY: value
      KEY=value
    Returns dict with lowercase keys.
    """
    data = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            k, v = line.split(":", 1)
        elif "=" in line:
            k, v = line.split("=", 1)
        else:
            continue
        data[k.strip().lower()] = v.strip()
    return data

def ensure_user(cur, phone: str, role: str, full_name=None):
    """Get or create user by phone. Returns row dict."""
    cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
    row = cur.fetchone()
    if row:
        if full_name and not row.get("full_name"):
            cur.execute("UPDATE users SET full_name=%s WHERE id=%s", (full_name, row["id"]))
            cur.execute("SELECT * FROM users WHERE id=%s", (row["id"],))
            return cur.fetchone()
        return row

    cur.execute(
        "INSERT INTO users (phone, role, full_name) VALUES (%s,%s,%s) RETURNING *",
        (phone, role, full_name)
    )
    return cur.fetchone()

def seats_left(trip):
    return int(trip["seats_total"]) - int(trip["seats_booked"])

def parse_date(d):
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except:
        return None

def parse_time(t):
    if not t:
        return None
    try:
        return datetime.strptime(t, "%H:%M").time()
    except:
        return None

def score_trip(trip, req_date, req_time):
    """
    Lower score is better (closest date/time).
    """
    score = 0

    if req_date and trip["trip_date"]:
        score += abs((trip["trip_date"] - req_date).days) * 1000
    else:
        score += 5000

    if req_time and trip["trip_time"]:
        t1 = trip["trip_time"].hour * 60 + trip["trip_time"].minute
        t2 = req_time.hour * 60 + req_time.minute
        score += abs(t1 - t2)
    else:
        score += 300

    return score

def find_best_trip(cur, origin, destination, req_date, req_time):
    cur.execute("""
      SELECT t.*, u.phone AS driver_phone, COALESCE(u.full_name,'Driver') AS driver_name
      FROM trips t
      JOIN users u ON u.id = t.driver_user_id
      WHERE LOWER(t.origin)=LOWER(%s)
        AND LOWER(t.destination)=LOWER(%s)
        AND t.status='active'
      ORDER BY t.created_at DESC
      LIMIT 50
    """, (origin, destination))

    trips = cur.fetchall()
    trips = [t for t in trips if seats_left(t) > 0]
    if not trips:
        return None

    trips.sort(key=lambda x: score_trip(x, req_date, req_time))
    return trips[0]

# ==========================
# WHATSAPP SEND
# ==========================

def send_message(to_number: str, message_text: str):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("⚠️ WhatsApp token/phone id missing; cannot send.")
        return

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text}
    }

    r = requests.post(url, json=payload, headers=headers, timeout=20)
    if r.status_code != 200:
        print("❌ WhatsApp send error:", r.text)

# ==========================
# BOT CORE
# ==========================

def process_message(text: str, phone: str) -> str:
    text = (text or "").strip()
    lower = text.lower()

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        state = user_states.get(phone, {}).get("state")

        # --------- STATE: REGISTER DRIVER ---------
        if state == "register_driver":
            data = parse_kv_lines(text)
            full_name = data.get("name") or data.get("full name")
            route = data.get("route", "")
            car = data.get("car") or data.get("vehicle")
            plate = data.get("plate") or data.get("plate number")

            route_from = None
            route_to = None
            if "-" in route:
                a, b = route.split("-", 1)
                route_from = a.strip()
                route_to = b.strip()

            if not (full_name and route_from and route_to and car and plate):
                return (
                    "❌ Incomplete details.\n\nReply like:\n"
                    "NAME: Ibrahim Musa\n"
                    "ROUTE: Daura - Katsina\n"
                    "CAR: Honda Accord\n"
                    "PLATE: KTS-456-AB"
                )

            user = ensure_user(cur, phone, "driver", full_name)

            cur.execute("""
                INSERT INTO driver_profiles (user_id, route_from, route_to, car_model, plate)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                    route_from=EXCLUDED.route_from,
                    route_to=EXCLUDED.route_to,
                    car_model=EXCLUDED.car_model,
                    plate=EXCLUDED.plate
                RETURNING *
            """, (user["id"], route_from, route_to, car, plate))

            conn.commit()
            user_states.pop(phone, None)
            return "✅ Driver registered successfully! Now send /post_trip"

        # --------- STATE: POST TRIP ---------
        if state == "post_trip":
            data = parse_kv_lines(text)
            origin = data.get("from") or data.get("origin")
            dest = data.get("to") or data.get("destination")
            trip_date = parse_date(data.get("date"))
            trip_time = parse_time(data.get("time"))
            seats = data.get("seats")
            price = data.get("price")

            if not (origin and dest and seats and price):
                return (
                    "❌ Incomplete trip.\n\nReply like:\n"
                    "FROM: Daura\n"
                    "TO: Katsina\n"
                    "DATE: 2026-02-20\n"
                    "TIME: 06:30\n"
                    "SEATS: 3\n"
                    "PRICE: 2500"
                )

            cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
            driver = cur.fetchone()
            if not driver or driver["role"] != "driver":
                user_states.pop(phone, None)
                return "❌ You must register as a driver first using /register"

            cur.execute("""
                INSERT INTO trips (driver_user_id, origin, destination, trip_date, trip_time, seats_total, price_per_seat)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
            """, (driver["id"], origin, dest, trip_date, trip_time, int(seats), int(price)))

            trip = cur.fetchone()
            conn.commit()
            user_states.pop(phone, None)

            return (
                "🚗 Trip posted!\n"
                f"Trip ID: {trip['id']}\n"
                f"{trip['origin']} → {trip['destination']}\n"
                f"Seats: {trip['seats_total']} | Price: ₦{trip['price_per_seat']}"
            )

        # --------- STATE: REQUEST RIDE ---------
        if state == "request_ride":
            data = parse_kv_lines(text)
            origin = data.get("from") or data.get("origin")
            dest = data.get("to") or data.get("destination")
            req_date = parse_date(data.get("date"))
            req_time = parse_time(data.get("time"))

            if not (origin and dest):
                return (
                    "❌ Incomplete request.\n\nReply like:\n"
                    "FROM: Daura\n"
                    "TO: Katsina\n"
                    "DATE: 2026-02-20\n"
                    "TIME: 06:30"
                )

            passenger = ensure_user(cur, phone, "passenger", None)

            cur.execute("""
                INSERT INTO ride_requests (passenger_user_id, origin, destination, ride_date, ride_time)
                VALUES (%s,%s,%s,%s,%s)
                RETURNING *
            """, (passenger["id"], origin, dest, req_date, req_time))
            req = cur.fetchone()

            best = find_best_trip(cur, origin, dest, req_date, req_time)

            if not best:
                conn.commit()
                user_states.pop(phone, None)
                return "🚕 Ride request submitted! No match yet — try again later."

            cur.execute("""
                UPDATE ride_requests
                SET matched=TRUE, matched_trip_id=%s
                WHERE id=%s
            """, (best["id"], req["id"]))

            conn.commit()
            user_states.pop(phone, None)

            return (
                "✅ Ride matched!\n"
                f"Trip ID: {best['id']}\n"
                f"Route: {best['origin']} → {best['destination']}\n"
                f"Driver: {best['driver_name']}\n"
                f"Price/Seat: ₦{best['price_per_seat']}\n"
                f"Seats left: {seats_left(best)}"
            )

        # --------- COMMANDS ---------
        if lower == "/help":
            return (
                "🚗 ROUTERIDER BOT COMMANDS\n\n"
                "/register  - Register as driver\n"
                "/post_trip - Post a new trip (drivers)\n"
                "/ride      - Request a ride (passengers)\n"
                "/my_stats  - View stats\n"
                "/complete <trip_id> - Mark trip completed\n"
            )

        if lower == "/register":
            user_states[phone] = {"state": "register_driver"}
            return (
                "✅ DRIVER REGISTRATION\n\n"
                "Reply with:\n"
                "NAME:\n"
                "ROUTE: Daura - Katsina\n"
                "CAR:\n"
                "PLATE:"
            )

        if lower == "/post_trip":
            cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
            u = cur.fetchone()
            if not u or u["role"] != "driver":
                return "❌ You must register as a driver first using /register"

            user_states[phone] = {"state": "post_trip"}
            return (
                "🚗 POST TRIP\n\n"
                "Reply with:\n"
                "FROM:\n"
                "TO:\n"
                "DATE: 2026-02-20\n"
                "TIME: 06:30\n"
                "SEATS:\n"
                "PRICE:"
            )

        if lower == "/ride":
            user_states[phone] = {"state": "request_ride"}
            return (
                "🧍 REQUEST A RIDE\n\n"
                "Reply with:\n"
                "FROM:\n"
                "TO:\n"
                "DATE: 2026-02-20\n"
                "TIME: 06:30"
            )

        if lower == "/my_stats":
            cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
            u = cur.fetchone()
            if not u:
                return "Not registered yet. Use /register (drivers) or /ride (passengers)."

            if u["role"] == "driver":
                cur.execute("SELECT COUNT(*) AS c FROM trips WHERE driver_user_id=%s", (u["id"],))
                trips_count = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM trips WHERE driver_user_id=%s AND status='completed'", (u["id"],))
                completed = cur.fetchone()["c"]
                return f"📊 DRIVER STATS\nTrips posted: {trips_count}\nCompleted: {completed}"
            else:
                cur.execute("SELECT COUNT(*) AS c FROM ride_requests WHERE passenger_user_id=%s", (u["id"],))
                reqs = cur.fetchone()["c"]
                return f"📊 PASSENGER STATS\nRide requests: {reqs}"

        if lower.startswith("/complete"):
            parts = lower.split()
            if len(parts) != 2 or not parts[1].isdigit():
                return "Usage: /complete 1"

            trip_id = int(parts[1])

            cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
            u = cur.fetchone()
            if not u or u["role"] != "driver":
                return "❌ Only drivers can complete trips."

            cur.execute("""
                UPDATE trips SET status='completed'
                WHERE id=%s AND driver_user_id=%s
            """, (trip_id, u["id"]))

            conn.commit()
            if cur.rowcount == 0:
                return "❌ Trip not found (or not yours)."
            return "✅ Trip marked as completed."

        return "Send /help to see commands."

    except Exception as e:
        print("❌ Bot error:", e)
        return "⚠️ Something went wrong. Try again."
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# ==========================
# WEBHOOK
# ==========================

@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    data = request.get_json(silent=True) or {}

    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        if "messages" in value:
            message = value["messages"][0]
            from_number = message.get("from")
            message_text = (message.get("text") or {}).get("body", "")

            print(f"📩 From {from_number}: {message_text}")

            reply = process_message(message_text, from_number)
            if reply:
                send_message(from_number, reply)

    except Exception as e:
        print("❌ Webhook processing error:", e)

    return jsonify({"status": "ok"}), 200

@app.route("/")
def home():
    return "RouteRider Bot Running 🚗", 200

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
