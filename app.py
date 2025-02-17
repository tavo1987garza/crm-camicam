from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # 🔹 Permitir conexiones de cualquier origen

# 📌 Conectar a la base de datos PostgreSQL
def conectar_db():
    try:
        DATABASE_URL = os.environ.get("DATABASE_URL")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        return conn
    except Exception as e:
        print("❌ Error al conectar con la base de datos:", str(e))
        return None

# 📌 Ruta principal
@app.route("/")
def home():
    return "¡CRM de Camicam funcionando!"

# 📌 Endpoint para recibir mensajes desde WhatsApp
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    datos = request.json
    plataforma = datos.get("plataforma")
    remitente = datos.get("remitente")
    mensaje = datos.get("mensaje")

    if not plataforma or not remitente or not mensaje:
        return jsonify({"error": "Faltan datos"}), 400

    # Guardar mensaje en la base de datos
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo) VALUES (%s, %s, %s, 'Nuevo', 'recibido')",
        (plataforma, remitente, mensaje)
    )
    conn.commit()
    conn.close()

    # 🔹 Emitir evento para actualizar el chat en tiempo real
    socketio.emit("nuevo_mensaje", {
        "plataforma": plataforma,
        "remitente": remitente,
        "mensaje": mensaje
    })

    return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200

# 📌 Endpoint para responder al cliente
CAMIBOT_API_URL = "https://cami-bot-7d4110f9197c.herokuapp.com/enviar_mensaje"

@app.route("/enviar_respuesta", methods=["POST"])
def enviar_respuesta():
    datos = request.json
    remitente = datos.get("remitente")
    mensaje = datos.get("mensaje")

    if not remitente or not mensaje:
        return jsonify({"error": "Faltan datos"}), 400

    # Guardar mensaje en la base de datos
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo) VALUES (%s, %s, %s, NULL, 'enviado')",
        ("CRM", remitente, mensaje)
    )
    conn.commit()
    conn.close()

    # 🔹 Enviar el mensaje a WhatsApp a través de Camibot
    payload = {
        "telefono": remitente,
        "mensaje": mensaje
    }
    respuesta = requests.post(CAMIBOT_API_URL, json=payload)

    if respuesta.status_code == 200:
        return jsonify({"mensaje": "Respuesta enviada correctamente a WhatsApp"}), 200
    else:
        return jsonify({"error": f"Error en Camibot: {respuesta.status_code} - {respuesta.text}"}), 500

# 📌 Endpoint para obtener mensajes
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    try:
        conn = conectar_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM mensajes ORDER BY fecha DESC")
        mensajes = cursor.fetchall()
        conn.close()
        return jsonify(mensajes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 📌 Endpoint para obtener mensajes de un chat específico
@app.route("/mensajes_chat", methods=["GET"])
def obtener_mensajes_chat():
    try:
        remitente = request.args.get("id")
        if not remitente:
            return jsonify({"error": "Falta el ID del remitente"}), 400

        conn = conectar_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM mensajes WHERE remitente = %s ORDER BY fecha ASC", (remitente,))
        mensajes = cursor.fetchall()
        conn.close()

        return jsonify(mensajes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
