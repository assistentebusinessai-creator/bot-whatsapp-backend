from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import os
from datetime import datetime
import sqlite3
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


class DatabaseRichieste:

    def __init__(self):
        # Il database sarà salvato nella stessa cartella del progetto
        self.db_path = 'richieste_officina.db'
        self.crea_tabella()

    def crea_tabella(self):
        """Crea la tabella se non esiste"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS richieste (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero_cliente TEXT NOT NULL,
                auto TEXT,
                problema TEXT,
                problema_cod TEXT,
                urgenza TEXT,
                spie_comportamenti TEXT,
                preferenza_orario TEXT,
                tipo_intervento TEXT,
                diagnosi_controllo TEXT,
                categoria TEXT,
                data_richiesta TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stato TEXT DEFAULT 'nuova'
            )
        ''')
        conn.commit()
        conn.close()

    def salva_richiesta(self, numero_cliente, dati, categoria):
        """Salva una nuova richiesta nel database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                '''
                INSERT INTO richieste 
                (numero_cliente, auto, problema, problema_cod, urgenza, 
                 spie_comportamenti, preferenza_orario, tipo_intervento, 
                 diagnosi_controllo, categoria, data_richiesta, stato)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (numero_cliente, dati.get('auto'), dati.get('problema'),
                  dati.get('problema_cod'), dati.get('urgenza'),
                  dati.get('spie_comportamenti'),
                  dati.get('preferenza_orario'), dati.get('tipo_intervento'),
                  dati.get('diagnosi_controllo'), categoria,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'nuova'))
            conn.commit()
            print(f"✅ Richiesta salvata per {numero_cliente}")
        except Exception as e:
            print(f"❌ Errore salvataggio: {e}")
        finally:
            conn.close()

    def leggi_tutte_richieste(self):
        """Legge tutte le richieste dal database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, numero_cliente, auto, problema, urgenza, 
                   spie_comportamenti, preferenza_orario, tipo_intervento,
                   diagnosi_controllo, categoria, data_richiesta, stato
            FROM richieste 
            ORDER BY data_richiesta DESC
        ''')
        richieste = cursor.fetchall()
        conn.close()
        return richieste

    def leggi_richieste_nuove(self):
        """Legge solo le richieste con stato 'nuova'"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, numero_cliente, auto, problema, urgenza,
                   spie_comportamenti, preferenza_orario, tipo_intervento,
                   diagnosi_controllo, categoria, data_richiesta, stato
            FROM richieste 
            WHERE stato = 'nuova'
            ORDER BY data_richiesta DESC
        ''')
        richieste = cursor.fetchall()
        conn.close()
        return richieste

    def aggiorna_stato(self, id_richiesta, nuovo_stato):
        """Aggiorna lo stato di una richiesta (es: 'nuova' -> 'lavorata' -> 'completata')"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE richieste 
            SET stato = ? 
            WHERE id = ?
        ''', (nuovo_stato, id_richiesta))
        conn.commit()
        conn.close()

    def elimina_richiesta(self, id_richiesta):
        """Elimina una richiesta dal database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM richieste WHERE id = ?', (id_richiesta, ))
        conn.commit()
        conn.close()

    def conta_richieste_nuove(self):
        """Conta quante richieste nuove ci sono"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM richieste WHERE stato = "nuova"')
        count = cursor.fetchone()[0]
        conn.close()
        return count


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

                # Se è urgenza (1), chiedi dettagli urgenza
                if messaggio == '1':
                    conv['step'] = 3
                    return self.steps[3]

                # Se è tagliando (2), prima domanda
                elif messaggio == '2':
                    conv['step'] = 4
                    return "Hai notato qualche spia accesa sul cruscotto o comportamenti strani dell'auto?"

                # Se è preventivo (3), prima domanda
                elif messaggio == '3':
                    conv['step'] = 6
                    return "Di che tipo di intervento si tratta? (es: freni, gomme, carrozzeria, climatizzatore...)"
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

        # STEP 4: Prima domanda tagliando (NUOVO)
        elif step_corrente == 4:
            conv['dati']['spie_comportamenti'] = messaggio
            conv['step'] = 5
            return "Hai preferenze di orario? Mattina o pomeriggio?"

        # STEP 5: Seconda domanda tagliando (NUOVO)
        elif step_corrente == 5:
            conv['dati']['preferenza_orario'] = messaggio
            return self.chiudi_conversazione(numero_cliente, None)

        # STEP 6: Prima domanda preventivo (NUOVO)
        elif step_corrente == 6:
            conv['dati']['tipo_intervento'] = messaggio
            conv['step'] = 7
            return "Hai già una diagnosi o serve prima un controllo per capire il problema?"

        # STEP 7: Seconda domanda preventivo (NUOVO)
        elif step_corrente == 7:
            conv['dati']['diagnosi_controllo'] = messaggio
            return self.chiudi_conversazione(numero_cliente, None)

        else:
            return "Scusa, non ho capito. Riprova."

    def chiudi_conversazione(self, numero_cliente, urgenza):
        """Chiude la conversazione e salva la richiesta"""
        conv = conversazioni[numero_cliente]
        dati = conv['dati']

        # Classifica richiesta
        categoria = self.classifica_richiesta(dati.get('problema_cod'),
                                              urgenza)

        # SALVA NEL DATABASE
        db = DatabaseRichieste()
        db.salva_richiesta(numero_cliente, dati, categoria)

        # Prepara il messaggio riepilogativo per il titolare
        riepilogo = f"📋 NUOVA RICHIESTA\n\n"
        riepilogo += f"🚗 Auto: {dati.get('auto', 'N/D')}\n"
        riepilogo += f"❗ Problema: {dati.get('problema', 'N/D')}\n"

        # Aggiungi dettagli specifici in base al tipo di problema
        if dati.get('problema_cod') == '1':
            riepilogo += f"🚨 Urgenza: {urgenza}\n"
            riepilogo += f"🏷️ Categoria: {categoria}\n"

        elif dati.get('problema_cod') == '2':
            riepilogo += f"💡 Spie/Comportamenti: {dati.get('spie_comportamenti', 'N/D')}\n"
            riepilogo += f"🕐 Preferenza orario: {dati.get('preferenza_orario', 'N/D')}\n"
            riepilogo += f"🏷️ Categoria: {categoria}\n"

        elif dati.get('problema_cod') == '3':
            riepilogo += f"🔧 Tipo intervento: {dati.get('tipo_intervento', 'N/D')}\n"
            riepilogo += f"📋 Diagnosi: {dati.get('diagnosi_controllo', 'N/D')}\n"
            riepilogo += f"🏷️ Categoria: {categoria}\n"

        riepilogo += f"\n📱 Cliente: {numero_cliente}"

        # NOTIFICA AL TITOLARE
        self.invia_notifica_titolare(riepilogo)

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

    def visualizza_richieste_titolare():
        """Funzione per mostrare le richieste al titolare"""
        db = DatabaseRichieste()

        # Mostra solo le nuove
        richieste_nuove = db.leggi_richieste_nuove()

        print(f"\n{'='*50}")
        print(f"📋 RICHIESTE NUOVE: {len(richieste_nuove)}")
        print(f"{'='*50}\n")

        for richiesta in richieste_nuove:
            id_r, numero, auto, problema, urgenza, spie, orario, intervento, diagnosi, categoria, data, stato = richiesta

            print(f"ID: {id_r}")
            print(f"📱 Cliente: {numero}")
            print(f"🚗 Auto: {auto}")
            print(f"❗ Problema: {problema}")

            if urgenza:
                print(f"🚨 Urgenza: {urgenza}")
            if spie:
                print(f"💡 Spie: {spie}")
            if orario:
                print(f"🕐 Orario: {orario}")
            if intervento:
                print(f"🔧 Intervento: {intervento}")
            if diagnosi:
                print(f"📋 Diagnosi: {diagnosi}")

                print(f"🏷️ {categoria}")
                print(f"📅 Data: {data}")
                print(f"{'-'*50}\n")


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
    
