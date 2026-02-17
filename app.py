import os
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Meta WhatsApp Cloud API credentials
WHATSAPP_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN')
PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')
VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')

# Temporary in-memory user state storage
user_states = {}
registered_drivers = {}


@app.route('/webhook/whatsapp', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print('✅ Webhook verified!')
            return challenge, 200
        else:
            print('❌ Webhook verification failed')
            return 'Forbidden', 403

    elif request.method == 'POST':
        data = request.get_json()

        try:
            entry = data['entry'][0]
            changes = entry['changes'][0]
            value = changes['value']

            if 'messages' in value:
                message = value['messages'][0]
                from_number = message['from']
                message_text = message['text']['body']

                print(f'📩 Message from {from_number}: {message_text}')

                response_text = process_message(message_text, from_number)

                if response_text:
                    send_message(from_number, response_text)

        except Exception as e:
            print(f'❌ Error processing message: {e}')

        return jsonify({'status': 'ok'}), 200


def process_message(message_text, phone_number):
    command = message_text.strip()
    lower_command = command.lower()

    # ===============================
    # CHECK IF USER IS IN A STATE
    # ===============================

    if user_states.get(phone_number) == "awaiting_registration":
        registered_drivers[phone_number] = command
        user_states.pop(phone_number)

        return "🎉 Registration successful!\n\nYou are now a registered driver.\nUse /post_trip to post your first trip."

    if user_states.get(phone_number) == "awaiting_trip":
        user_states.pop(phone_number)

        return "🚗 Trip posted successfully!\nPassengers on your route will be notified."

    # ===============================
    # COMMAND HANDLING
    # ===============================

    if lower_command == '/help':
        return """🚗 ROUTERIDER BOT COMMANDS

/register - Register as driver
/post_trip - Post a new trip
/my_stats - View your statistics
/complete [trip_id] - Mark trip as complete
/help - Show this help text"""

    elif lower_command == '/register':
        user_states[phone_number] = "awaiting_registration"
        return """✅ DRIVER REGISTRATION

Reply with your details in this format:

NAME: Your Full Name
ROUTE: Daura - Katsina
CAR: Toyota Corolla
PLATE: ABC-123-XY"""

    elif lower_command == '/post_trip':
        if phone_number not in registered_drivers:
            return "❌ You must register first.\nUse /register to begin."

        user_states[phone_number] = "awaiting_trip"
        return """🚗 POST NEW TRIP

Reply with trip details:

DATE: 2026-02-17
TIME: 06:30
SEATS: 3
PRICE: 2500"""

    elif lower_command == '/my_stats':
        if phone_number not in registered_drivers:
            return "❌ You are not registered.\nUse /register first."

        return """📊 YOUR DRIVER STATS

Total Trips: 0
Seats Filled: 0
Earnings This Month: ₦0
Rating: Not yet rated"""

    elif lower_command.startswith('/complete'):
        if phone_number not in registered_drivers:
            return "❌ You are not registered."

        return "✅ Trip marked as complete!\nEarnings added."

    else:
        return "👋 Welcome to RouteRider!\nSend /help to see available commands."


def send_message(to_number, message_text):
    url = f'https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages'

    headers = {
        'Authorization': f'Bearer {WHATSAPP_TOKEN}',
        'Content-Type': 'application/json'
    }

    payload = {
        'messaging_product': 'whatsapp',
        'to': to_number,
        'type': 'text',
        'text': {'body': message_text}
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 200:
        print(f'✅ Message sent to {to_number}')
    else:
        print(f'❌ Failed to send message: {response.text}')


@app.route('/')
def home():
    return 'RouteRider WhatsApp Bot is running! 🚗', 200


@app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
