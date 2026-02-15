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

@app.route('/webhook/whatsapp', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Webhook verification
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
        # Handle incoming messages
        data = request.get_json()
        
        try:
            # Extract message details
            entry = data['entry'][0]
            changes = entry['changes'][0]
            value = changes['value']
            
            if 'messages' in value:
                message = value['messages'][0]
                from_number = message['from']
                message_text = message['text']['body']
                
                print(f'📩 Message from {from_number}: {message_text}')
                
                # Process command
                response_text = process_command(message_text, from_number)
                
                # Send response
                send_message(from_number, response_text)
                
        except Exception as e:
            print(f'❌ Error processing message: {e}')
        
        return jsonify({'status': 'ok'}), 200

def process_command(message_text, phone_number):
    """Process bot commands"""
    
    command = message_text.strip().lower()
    
    if command == '/help':
        return """🚗 ROUTERIDER BOT COMMANDS

/register - Register as driver
/post_trip - Post a new trip
/my_stats - View your statistics
/complete [trip_id] - Mark trip as complete
/help - Show this help text

Need help? Contact support."""
    
    elif command == '/register':
        return """✅ DRIVER REGISTRATION

Reply with your details in this format:

NAME: Your Full Name
ROUTE: Daura - Katsina
CAR: Toyota Corolla
PLATE: ABC-123-XY

Example:
NAME: Ibrahim Musa
ROUTE: Daura - Katsina
CAR: Honda Accord
PLATE: KTS-456-AB"""
    
    elif command == '/post_trip':
        return """🚗 POST NEW TRIP

Reply with trip details:

DATE: 2026-02-17
TIME: 06:30
SEATS: 3
PRICE: 2500

We'll notify passengers on your route!"""
    
    elif command == '/my_stats':
        return """📊 YOUR DRIVER STATS

Total Trips: 0
Seats Filled: 0
Earnings This Month: ₦0
Rating: Not yet rated

Complete trips to build your stats!"""
    
    elif command.startswith('/complete'):
        return """✅ TRIP COMPLETED

Trip marked as complete!
Earnings added to your account.

Post your next trip with /post_trip"""
    
    else:
        return """👋 Welcome to RouteRider!

I didn't understand that command.
Send /help to see available commands."""

def send_message(to_number, message_text):
    """Send WhatsApp message via Meta Cloud API"""
    
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
    app.run(host='0.0.0.0', port=port, debug=True)