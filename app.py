from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO


app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # 🔹 Eliminamos async_mode

# 📌 Ruta raiz 
@app.route("/")
def home():
    return "¡CRM de Camicam funcionando!"

# 📌 Función para conectar a la base de datos
import os
import psycopg2
from psycopg2.extras import RealDictCursor

def conectar_db():
    try:
        DATABASE_URL = os.environ.get("DATABASE_URL")
        
        # Asegurar compatibilidad con psycopg2 (Heroku usa "postgres://", pero psycopg2 necesita "postgresql://")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        return conn

    except Exception as e:
        print("❌ Error al conectar con la base de datos:", str(e))
        return None


# Ruta para Leads
@app.route("/leads", methods=["GET"])
def obtener_leads():
    return jsonify([])  # Devuelve una lista vacía para evitar errores


# 📌 Endpoint para recibir mensajes desde whatsapp
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    datos = request.json
    plataforma = datos.get("plataforma")
    remitente = datos.get("remitente")
    mensaje = datos.get("mensaje")

    if not plataforma or not remitente or not mensaje:
        return jsonify({"error": "Faltan datos"}), 400

    # Guardar el mensaje en la base de datos
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo) VALUES (%s, %s, %s, 'Nuevo', 'recibido')",
                   (plataforma, remitente, mensaje))
    conn.commit()
    conn.close()

    # 🔹 Emitir evento de nuevo mensaje en tiempo real
    socketio.emit("nuevo_mensaje", {
        "plataforma": plataforma,
        "remitente": remitente,
        "mensaje": mensaje
    })

    return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200

# 📌 Endpoint para contestar al cliente
import requests
import os

# URL de Camibot (Reemplazar con la correcta)
CAMIBOT_API_URL = "https://cami-bot-7d4110f9197c.herokuapp.com/enviar_mensaje"  # Asegúrate de que sea la URL correcta

@app.route("/enviar_respuesta", methods=["POST"])
def enviar_respuesta():
    try:
        datos = request.json
        remitente = datos.get("remitente")
        mensaje = datos.get("mensaje")

        if not remitente or not mensaje:
            return jsonify({"error": "Faltan datos"}), 400

        # Guardar el mensaje en la base de datos como "enviado"
        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo) VALUES (%s, %s, %s, NULL, 'enviado')",
            ("CRM", remitente, mensaje)
        )
        conn.commit()
        conn.close()

        # 🔹 Enviar el mensaje a Camibot para que lo reenvíe a WhatsApp
        payload = {
            "telefono": remitente,
            "mensaje": mensaje
        }

        respuesta = requests.post(CAMIBOT_API_URL, json=payload)

        if respuesta.status_code == 200:
            return jsonify({"mensaje": "Respuesta enviada correctamente a WhatsApp"}), 200
        else:
            return jsonify({"error": f"Error en Camibot: {respuesta.status_code} - {respuesta.text}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500




# 📌 Endpoint para consultar los mensajes (con filtro opcional por estado)
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



# 📌 Endpoint para actualizar el estado de un mensaje
@app.route("/actualizar_estado", methods=["POST"])
def actualizar_estado():
    datos = request.json
    mensaje_id = datos.get("id")
    nuevo_estado = datos.get("estado")

    if not mensaje_id or nuevo_estado not in ["Nuevo", "En proceso", "Finalizado"]:
        return jsonify({"error": "Datos incorrectos"}), 400

    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE mensajes SET estado = ? WHERE id = ?", (nuevo_estado, mensaje_id))
    conn.commit()
    conn.close()

    return jsonify({"mensaje": "Estado actualizado correctamente"}), 200

# 📌 Endpoint para eliminar un mensaje por su ID
@app.route("/eliminar_mensaje", methods=["POST"])
def eliminar_mensaje():
    datos = request.json
    mensaje_id = datos.get("id")

    if not mensaje_id:
        return jsonify({"error": "Falta el ID del mensaje"}), 400

    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mensajes WHERE id = ?", (mensaje_id,))
    conn.commit()
    conn.close()
    # 🔹 Emitir evento de nuevo mensaje
    socketio.emit("nuevo_mensaje", {"plataforma": plataforma, "remitente": remitente, "mensaje": mensaje})
    return jsonify({"mensaje": "Mensaje eliminado correctamente"}), 200


# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)