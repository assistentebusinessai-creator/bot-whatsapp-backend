from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import os
from datetime import datetime
import json
import requests
from dotenv import load_dotenv
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
CORS(app)

# ==================== CONFIGURAZIONE ====================

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv(
    'TWILIO_WHATSAPP_NUMBER')  # es: whatsapp:+14155238886
FIREBASE_SERVER_KEY = os.getenv('FIREBASE_SERVER_KEY')  # Per notifiche push

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Database in-memory (usa Firebase/PostgreSQL in produzione)

conversazioni = {}
richieste = []

# ==================== BOT WHATSAPP LOGIC ====================


class BotOfficina:

    def __init__(self):
        self.steps = {
            0:
            "Ciao 👋\nSono l’assistente dell’officina.\nTi faccio 3 domande rapide per capire come aiutarti.",
            1:
            "🚗 Che auto hai?\n(Marca e modello)",
            2:
            "🔧 Che tipo di problema hai?\n\n1️⃣ Auto ferma / rumori strani\n2️⃣ Tagliando / controllo\n3️⃣ Preventivo / informazioni\n\nRispondi con 1, 2 o 3",
            3:
            "🚨 È urgente?\n\n1. Auto non parte\n2. Posso ancora circolare\n3. È solo un controllo\n\nRispondi con 1, 2 o 3",
        }

    def gestisci_messaggio(self, numero_cliente, messaggio):
        """Gestisce la conversazione step by step"""

        # Inizializza conversazione se nuova
        if numero_cliente not in conversazioni:
            conversazioni[numero_cliente] = {
                'step': 0,
                'dati': {},
                'timestamp': datetime.now()
            }

        conv = conversazioni[numero_cliente]
        step_corrente = conv['step']

        # STEP 0: Benvenuto
        if step_corrente == 0:
            conv['step'] = 1
            return self.steps[0] + "\n\n" + self.steps[1]

        # STEP 1: Raccolta auto
        elif step_corrente == 1:
            conv['dati']['auto'] = messaggio
            conv['step'] = 2
            return self.steps[2]

        # STEP 2: Tipo problema
        elif step_corrente == 2:
            problemi = {
                '1': 'Auto ferma / rumori strani',
                '2': 'Tagliando / controllo',
                '3': 'Preventivo / informazioni'
            }

            if messaggio in problemi:
                conv['dati']['problema'] = problemi[messaggio]
                conv['dati']['problema_cod'] = messaggio

                # Se è urgenza (1), chiedi dettagli
                if messaggio == '1':
                    conv['step'] = 3
                    return self.steps[3]
                else:
                    # Chiudi conversazione per appuntamenti/preventivi
                    return self.chiudi_conversazione(numero_cliente, None)

            else:
                return "Per favore rispondi con 1, 2 o 3"

        # STEP 3: Urgenza (solo se problema = 1)
        elif step_corrente == 3:
            urgenze = {
                '1': 'Auto non parte',
                '2': 'Posso ancora circolare',
                '3': 'È solo un controllo'
            }

            if messaggio in urgenze:
                conv['dati']['urgenza'] = urgenze[messaggio]
                return self.chiudi_conversazione(numero_cliente,
                                                 urgenze[messaggio])
            else:
                return "Per favore rispondi con 1, 2 o 3"

        return "Scusa, non ho capito. Riprova."

    def chiudi_conversazione(self, numero_cliente, urgenza):
        """Chiude la conversazione e salva la richiesta"""
        conv = conversazioni[numero_cliente]
        dati = conv['dati']

        # Classifica richiesta
        categoria = self.classifica_richiesta(dati.get('problema_cod'),
                                              urgenza)

        # Salva richiesta
        richiesta = {
            'id': len(richieste) + 1,
            'cliente': numero_cliente,
            'auto': dati.get('auto'),
            'problema': dati.get('problema'),
            'urgenza': urgenza,
            'categoria': categoria,
            'quando': dati.get('quando'),
            'dettaglio_preventivo': dati.get('dettaglio_preventivo'),
            'timestamp': datetime.now().isoformat(),
            'stato': 'nuova',
            'letto': False
        }

        richieste.append(richiesta)

        # NOTIFICA AL TITOLARE
        self.invia_notifica_titolare(richiesta)

        # Resetta conversazione
        del conversazioni[numero_cliente]

        return "Perfetto, abbiamo preso in carico la tua richiesta 👍\nTi ricontatteremo al più presto su questo numero."

    def classifica_richiesta(self, problema_cod, urgenza):
        """Classifica la richiesta in URGENTE, APPUNTAMENTO o PREVENTIVO"""
        if problema_cod == '1' and urgenza == 'Auto non parte':
            return 'URGENTE'
        elif problema_cod == '2':
            return 'APPUNTAMENTO'
        elif problema_cod == '3':
            return 'PREVENTIVO'
        else:
            return 'APPUNTAMENTO'

    def invia_notifica_titolare(self, richiesta):
        """Invia notifica push all'app del titolare"""

        # Se è URGENTE, invia notifica push immediata
        if richiesta['categoria'] == 'URGENTE':
            self.invia_push_notification(richiesta)

        # Per APPUNTAMENTO/PREVENTIVO: solo salvataggio (riepilogo giornaliero)
        print(
            f"📱 Richiesta salvata: {richiesta['categoria']} - {richiesta['auto']}"
        )

    def invia_push_notification(self, richiesta):
        """Invia notifica push Firebase all'app mobile"""

        if not FIREBASE_SERVER_KEY:
            print("⚠️ Firebase non configurato - notifica simulata")
            return

        url = "https://fcm.googleapis.com/fcm/send"

        headers = {
            "Authorization": f"Bearer {FIREBASE_SERVER_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "to": "/topics/titolare_officina",  # Topic per il titolare
            "priority": "high",
            "notification": {
                "title": "🚨 URGENZA",
                "body": f"{richiesta['auto']} - {richiesta['urgenza']}",
                "sound": "default",
                "badge": "1"
            },
            "data": {
                "richiesta_id": richiesta['id'],
                "categoria": richiesta['categoria'],
                "click_action": "OPEN_URGENZE"
            }
        }

        try:
            response = requests.post(url, headers=headers, json=payload)
            print(f"✅ Notifica inviata: {response.status_code}")
        except Exception as e:
            print(f"❌ Errore notifica: {e}")


# Inizializza bot

bot = BotOfficina()

# ==================== WEBHOOK WHATSAPP ====================


@app.route('/webhook/whatsapp', methods=['POST'])
def webhook_whatsapp():
    """Riceve messaggi WhatsApp da Twilio"""

    # Estrai dati da Twilio
    numero_cliente = request.form.get('From')  # es:    whatsapp:+393331234567
    messaggio = request.form.get('Body', '').strip()

    print(f"📩 Messaggio da {numero_cliente}: {messaggio}")

    # Processa con il bot
    risposta_bot = bot.gestisci_messaggio(numero_cliente, messaggio)

    # Invia risposta su WhatsApp
    twiml = MessagingResponse()
    twiml.message(risposta_bot)

    return str(twiml)


# ==================== API PER APP MOBILE ====================


@app.route('/api/richieste', methods=['GET'])
def get_richieste():
    """Ritorna tutte le richieste per l’app del titolare"""

    categoria = request.args.get(
        'categoria')  # URGENTE, APPUNTAMENTO,      PREVENTIVO
    stato = request.args.get('stato')  # nuova, risposta, completata

    richieste_filtrate = richieste

    if categoria:
        richieste_filtrate = [
            r for r in richieste_filtrate if r['categoria'] == categoria
        ]

    if stato:
        richieste_filtrate = [
            r for r in richieste_filtrate if r['stato'] == stato
        ]

    return jsonify(richieste_filtrate)


@app.route('/api/risposta', methods=['POST'])
def invia_risposta():
    """Riceve risposta dal titolare e la invia al cliente su WhatsApp"""

    data = request.json
    richiesta_id = data.get('richiesta_id')
    messaggio_risposta = data.get('messaggio')

    # Trova richiesta
    richiesta = next((r for r in richieste if r['id'] == richiesta_id), None)

    if not richiesta:
        return jsonify({'error': 'Richiesta non trovata'}), 404

    # Invia messaggio WhatsApp al cliente
    try:
        message = twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER,
                                                body=messaggio_risposta,
                                                to=richiesta['cliente'])

        # Aggiorna stato richiesta
        richiesta['stato'] = 'risposta'
        richiesta['risposta'] = messaggio_risposta
        richiesta['risposta_timestamp'] = datetime.now().isoformat()

        print(f"✅ Risposta inviata a {richiesta['cliente']}")

        return jsonify({'success': True, 'message_sid': message.sid})

    except Exception as e:
        print(f"❌ Errore invio WhatsApp: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/completa', methods=['POST'])
def completa_richiesta():
    """Segna richiesta come completata e invia messaggio automatico"""

    data = request.json
    richiesta_id = data.get('richiesta_id')

    richiesta = next((r for r in richieste if r['id'] == richiesta_id), None)

    if not richiesta:
        return jsonify({'error': 'Richiesta non trovata'}), 404

    # Messaggio automatico
    messaggio_completato = "🚗 La sua auto è pronta per il ritiro.\nGrazie per aver scelto la nostra officina!"

    try:
        message = twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER,
                                                body=messaggio_completato,
                                                to=richiesta['cliente'])

        richiesta['stato'] = 'completata'
        richiesta['completato_timestamp'] = datetime.now().isoformat()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== HEALTH CHECK ====================


@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'status': 'online',
        'service': 'Bot WhatsApp Officina',
        'richieste_totali': len(richieste),
        'conversazioni_attive': len(conversazioni)
    })


# ==================== AVVIO SERVER ====================

if __name__ == 'main':
    print("""
╔══════════════════════════════════════════╗
║  🚗 BOT WHATSAPP OFFICINA - AVVIATO     ║
╚══════════════════════════════════════════╝


    📱 Webhook: /webhook/whatsapp
    📊 API Richieste: /api/richieste
    💬 API Risposta: /api/risposta
    ✅ API Completa: /api/completa

    """)

app.run(host='0.0.0.0', port=5000, debug=True)
