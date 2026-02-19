import os
from datetime import datetime, date, time
import requests

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

import psycopg
from psycopg.rows import dict_row

load_dotenv()

app = Flask(__name__)

# Allow your Vite frontend to call this API
# Set FRONTEND_ORIGIN in Railway like: https://your-routerider-frontend.up.railway.app
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN}})

WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# ==========================
# DATABASE
# ==========================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set in Railway variables")
    # sslmode=require works for Railway public URLs; private also works.
    return psycopg.connect(DATABASE_URL, sslmode="require", row_factory=dict_row)

def init_db():
    """Safe to run on boot. Creates tables if missing."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
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
        print("✅ Database initialized OK")
    except Exception as e:
        print("❌ Database init failed:", e)

init_db()

# ==========================
# WhatsApp State Machine
# ==========================
user_states = {}  # phone -> {"state": "register_driver" | "post_trip" | "request_ride"}

# ==========================
# Helpers
# ==========================

def parse_kv_lines(text: str):
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

def json_safe(row: dict):
    """Convert date/time/datetime to string for JSON."""
    out = {}
    for k, v in row.items():
        if isinstance(v, (datetime, date, time)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out

def ensure_user(cur, phone: str, role: str, full_name=None):
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

# ==========================
# WhatsApp Send
# ==========================

def send_message(to_number: str, message_text: str):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("⚠️ WhatsApp token/phone id missing; cannot send.")
        return

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"body": message_text}}

    r = requests.post(url, json=payload, headers=headers, timeout=20)
    if r.status_code != 200:
        print("❌ WhatsApp send error:", r.text)

# ==========================
# WhatsApp Bot Core
# ==========================

def process_message(text: str, phone: str) -> str:
    text = (text or "").strip()
    lower = text.lower()

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                state = user_states.get(phone, {}).get("state")

                # DRIVER REGISTRATION
                if state == "register_driver":
                    data = parse_kv_lines(text)
                    full_name = data.get("name") or data.get("full name")
                    route = data.get("route", "")
                    car = data.get("car") or data.get("vehicle")
                    plate = data.get("plate") or data.get("plate number")

                    route_from = route_to = None
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
                    """, (user["id"], route_from, route_to, car, plate))

                    user_states.pop(phone, None)
                    return "✅ Driver registered successfully! Now send /post_trip"

                # POST TRIP
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
                    user_states.pop(phone, None)

                    return (
                        "🚗 Trip posted!\n"
                        f"Trip ID: {trip['id']}\n"
                        f"{trip['origin']} → {trip['destination']}\n"
                        f"Seats: {trip['seats_total']} | Price: ₦{trip['price_per_seat']}"
                    )

                # COMMANDS
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

                if lower.startswith("/complete"):
                    parts = lower.split()
                    if len(parts) != 2 or not parts[1].isdigit():
                        return "Usage: /complete 1"

                    trip_id = int(parts[1])

                    cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
                    u = cur.fetchone()
                    if not u or u["role"] != "driver":
                        return "❌ Only drivers can complete trips."

                    cur.execute("UPDATE trips SET status='completed' WHERE id=%s AND driver_user_id=%s",
                                (trip_id, u["id"]))
                    if cur.rowcount == 0:
                        return "❌ Trip not found (or not yours)."
                    return "✅ Trip marked as completed."

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
                        return "📊 PASSENGER STATS\n(coming soon)"

                return "Send /help to see commands."

    except Exception as e:
        print("❌ Bot error:", e)
        return "⚠️ Something went wrong. Try again."

# ==========================
# WhatsApp Webhook
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

            reply = process_message(message_text, from_number)
            if reply:
                send_message(from_number, reply)

    except Exception as e:
        print("❌ Webhook processing error:", e)

    return jsonify({"status": "ok"}), 200

# ==========================
# API ENDPOINTS (for routerider frontend)
# ==========================

@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok"}), 200

@app.post("/api/users/register")
def api_register_user():
    body = request.get_json(silent=True) or {}
    phone = body.get("phone")
    role = body.get("role", "passenger")
    full_name = body.get("full_name")

    if not phone or role not in ("driver", "passenger"):
        return jsonify({"ok": False, "error": "phone required, role must be driver|passenger"}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                user = ensure_user(cur, phone, role, full_name)
                return jsonify({"ok": True, "user": json_safe(user)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/users/me")
def api_me():
    phone = request.args.get("phone")
    if not phone:
        return jsonify({"ok": False, "error": "phone is required"}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
                u = cur.fetchone()
                if not u:
                    return jsonify({"ok": False, "error": "user not found"}), 404
                return jsonify({"ok": True, "user": json_safe(u)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/trips")
def api_list_trips():
    origin = request.args.get("origin")
    destination = request.args.get("destination")
    trip_date = request.args.get("date")  # YYYY-MM-DD
    status = request.args.get("status", "active")

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                q = """
                  SELECT t.*,
                         COALESCE(u.full_name, 'Driver') AS driver_name,
                         u.phone AS driver_phone
                  FROM trips t
                  JOIN users u ON u.id = t.driver_user_id
                  WHERE t.status=%s
                """
                params = [status]

                if origin:
                    q += " AND LOWER(t.origin)=LOWER(%s)"
                    params.append(origin)
                if destination:
                    q += " AND LOWER(t.destination)=LOWER(%s)"
                    params.append(destination)
                if trip_date:
                    q += " AND t.trip_date=%s"
                    params.append(trip_date)

                q += " ORDER BY t.created_at DESC LIMIT 200"
                cur.execute(q, tuple(params))
                trips = [json_safe(r) for r in cur.fetchall()]
                return jsonify({"ok": True, "trips": trips}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/trips/<int:trip_id>")
def api_get_trip(trip_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT t.*,
                         COALESCE(u.full_name, 'Driver') AS driver_name,
                         u.phone AS driver_phone
                  FROM trips t
                  JOIN users u ON u.id = t.driver_user_id
                  WHERE t.id=%s
                """, (trip_id,))
                t = cur.fetchone()
                if not t:
                    return jsonify({"ok": False, "error": "trip not found"}), 404
                return jsonify({"ok": True, "trip": json_safe(t)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/trips")
def api_create_trip():
    body = request.get_json(silent=True) or {}
    driver_phone = body.get("driver_phone")
    origin = body.get("origin")
    destination = body.get("destination")
    trip_date = body.get("trip_date")  # YYYY-MM-DD
    trip_time = body.get("trip_time")  # HH:MM
    seats_total = body.get("seats_total")
    price_per_seat = body.get("price_per_seat")

    if not all([driver_phone, origin, destination, seats_total, price_per_seat]):
        return jsonify({"ok": False, "error": "missing fields"}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE phone=%s", (driver_phone,))
                driver = cur.fetchone()
                if not driver or driver["role"] != "driver":
                    return jsonify({"ok": False, "error": "driver not found / not a driver"}), 404

                cur.execute("""
                  INSERT INTO trips (driver_user_id, origin, destination, trip_date, trip_time, seats_total, price_per_seat)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)
                  RETURNING *
                """, (
                    driver["id"],
                    origin,
                    destination,
                    parse_date(trip_date),
                    parse_time(trip_time),
                    int(seats_total),
                    int(price_per_seat),
                ))
                t = cur.fetchone()
                return jsonify({"ok": True, "trip": json_safe(t)}), 201
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/bookings")
def api_create_booking():
    body = request.get_json(silent=True) or {}
    trip_id = body.get("trip_id")
    passenger_phone = body.get("passenger_phone")
    seats = int(body.get("seats", 1))

    if not trip_id or not passenger_phone:
        return jsonify({"ok": False, "error": "trip_id and passenger_phone required"}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                passenger = ensure_user(cur, passenger_phone, "passenger", None)

                cur.execute("SELECT * FROM trips WHERE id=%s", (int(trip_id),))
                trip = cur.fetchone()
                if not trip:
                    return jsonify({"ok": False, "error": "trip not found"}), 404
                if trip["status"] != "active":
                    return jsonify({"ok": False, "error": "trip not active"}), 400
                if seats_left(trip) < seats:
                    return jsonify({"ok": False, "error": "not enough seats left"}), 400

                amount_paid = seats * int(trip["price_per_seat"])

                cur.execute("""
                  INSERT INTO bookings (trip_id, passenger_user_id, seats, amount_paid, status)
                  VALUES (%s,%s,%s,%s,'pending')
                  RETURNING *
                """, (int(trip_id), passenger["id"], seats, amount_paid))
                booking = cur.fetchone()

                cur.execute("""
                  UPDATE trips
                  SET seats_booked = seats_booked + %s
                  WHERE id=%s
                """, (seats, int(trip_id)))

                return jsonify({"ok": True, "booking": json_safe(booking)}), 201
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/bookings")
def api_list_bookings():
    phone = request.args.get("phone")
    trip_id = request.args.get("trip_id")

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                q = """
                  SELECT b.*,
                         t.origin, t.destination, t.trip_date, t.trip_time,
                         COALESCE(d.full_name, 'Driver') AS driver_name
                  FROM bookings b
                  JOIN trips t ON t.id = b.trip_id
                  JOIN users d ON d.id = t.driver_user_id
                  WHERE 1=1
                """
                params = []

                if phone:
                    cur.execute("SELECT id FROM users WHERE phone=%s", (phone,))
                    u = cur.fetchone()
                    if not u:
                        return jsonify({"ok": True, "bookings": []}), 200
                    q += " AND b.passenger_user_id=%s"
                    params.append(u["id"])

                if trip_id:
                    q += " AND b.trip_id=%s"
                    params.append(int(trip_id))

                q += " ORDER BY b.created_at DESC LIMIT 200"
                cur.execute(q, tuple(params))
                rows = [json_safe(r) for r in cur.fetchall()]
                return jsonify({"ok": True, "bookings": rows}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.patch("/api/bookings/<int:booking_id>")
def api_update_booking(booking_id: int):
    body = request.get_json(silent=True) or {}
    status = body.get("status")  # confirmed/cancelled/completed

    if status not in ("confirmed", "cancelled", "completed"):
        return jsonify({"ok": False, "error": "status must be confirmed|cancelled|completed"}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET status=%s WHERE id=%s RETURNING *", (status, booking_id))
                b = cur.fetchone()
                if not b:
                    return jsonify({"ok": False, "error": "booking not found"}), 404
                return jsonify({"ok": True, "booking": json_safe(b)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
# Basic Pages
# ==========================

@app.get("/")
def home():
    return "RouteRider Bot + API Running 🚗", 200

@app.get("/health")
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
