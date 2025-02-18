from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# 📌 Ruta raíz
@app.route("/")
def home():
    return "¡CRM de Camicam funcionando!"

# 📌 Función para conectar a la base de datos
def conectar_db():
    conn = None
    try:
        DATABASE_URL = os.environ.get("DATABASE_URL")
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        return conn
    except Exception as e:
        print("❌ Error al conectar con la base de datos:", str(e))
    return None

# 📌 Ruta para obtener Leads
@app.route("/leads", methods=["GET"])
def obtener_leads():
    try:
        conn = conectar_db()
        if conn:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM leads ORDER BY estado")
            leads = cursor.fetchall()
            conn.close()
            return jsonify(leads if leads else [])  # Siempre devolver un array
    except Exception as e:
        print("❌ Error en /leads:", str(e))
        return jsonify([])


# 📌 Endpoint para recibir mensajes desde WhatsApp
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    datos = request.json
    plataforma = datos.get("plataforma")
    remitente = datos.get("remitente")
    mensaje = datos.get("mensaje")

    if not plataforma or not remitente or not mensaje:
        return jsonify({"error": "Faltan datos"}), 400

    conn = conectar_db()
    if conn:
        try:
            cursor = conn.cursor()
            
            # Verificar si el remitente ya es un lead
            cursor.execute("SELECT id FROM leads WHERE telefono = %s", (remitente,))
            lead = cursor.fetchone()
            
            if not lead:
                # Crear lead automáticamente
                cursor.execute("INSERT INTO leads (nombre, telefono, estado) VALUES (%s, %s, %s) RETURNING id",
                               (remitente, remitente, "Contacto Inicial"))
                lead_id = cursor.fetchone()[0]
                conn.commit()
                socketio.emit("nuevo_lead", {"id": lead_id, "nombre": remitente, "telefono": remitente, "estado": "Contacto Inicial"})
            
            # Guardar mensaje en la tabla "mensajes"
            cursor.execute("INSERT INTO mensajes (plataforma, remitente, mensaje, estado) VALUES (%s, %s, %s, %s)",
                           (plataforma, remitente, mensaje, "Nuevo"))
            conn.commit()
        finally:
            conn.close()
    
    socketio.emit("nuevo_mensaje", {"plataforma": plataforma, "remitente": remitente, "mensaje": mensaje})
    return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200



# 📌 Crear un nuevo lead manualmente
@app.route("/crear_lead", methods=["POST"])
def crear_lead():
    try:
        datos = request.json
        nombre = datos.get("nombre")
        telefono = datos.get("telefono")
        estado = "Contacto Inicial"
        
        if not nombre or not telefono:
            return jsonify({"error": "Faltan datos"}), 400

        conn = conectar_db()
        if conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO leads (nombre, telefono, estado) VALUES (%s, %s, %s) RETURNING id",
                           (nombre, telefono, estado))
            lead_id = cursor.fetchone()[0]
            conn.commit()
            conn.close()
        
        socketio.emit("nuevo_lead", {
            "id": lead_id,
            "nombre": nombre,
            "telefono": telefono,
            "estado": estado
        })

        return jsonify({"mensaje": "Lead creado correctamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 📌 Endpoint para actualizar estado de Lead
@app.route("/cambiar_estado_lead", methods=["POST"])
def cambiar_estado_lead():
    try:
        datos = request.json
        lead_id = datos.get("id")
        nuevo_estado = datos.get("estado")
        if not lead_id or nuevo_estado not in ["Contacto Inicial", "En proceso", "Seguimiento", "Cliente", "No cliente"]:
            return jsonify({"error": "Datos incorrectos"}), 400
        conn = conectar_db()
        if conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE leads SET estado = %s WHERE id = %s", (nuevo_estado, lead_id))
            conn.commit()
            conn.close()
        return jsonify({"mensaje": "Estado actualizado correctamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 📌 Ruta para eliminar un lead
@app.route("/eliminar_lead", methods=["POST"])
def eliminar_lead():
    try:
        datos = request.json
        lead_id = datos.get("id")
        conn = conectar_db()
        if conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM leads WHERE id = %s", (lead_id,))
            conn.commit()
            conn.close()
        return jsonify({"mensaje": "Lead eliminado correctamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 📌 Enviar respuesta al cliente
CAMIBOT_API_URL = "https://cami-bot-7d4110f9197c.herokuapp.com/enviar_mensaje"

@app.route("/enviar_respuesta", methods=["POST"])
def enviar_respuesta():
    try:
        datos = request.json
        remitente = datos.get("remitente")
        mensaje = datos.get("mensaje")

        if not remitente or not mensaje:
            return jsonify({"error": "Faltan datos"}), 400

        print(f"📩 Enviando mensaje a {remitente}: {mensaje}")

        conn = conectar_db()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo) VALUES (%s, %s, %s, NULL, 'enviado')",
                               ("CRM", remitente, mensaje))
                conn.commit()
            finally:
                conn.close()

        # Enviar mensaje a Camibot
        payload = {"telefono": remitente, "mensaje": mensaje}
        respuesta = requests.post(CAMIBOT_API_URL, json=payload)

        print(f"✅ Respuesta de Camibot: {respuesta.status_code}, {respuesta.text}")

        if respuesta.status_code == 200:
            return jsonify({"mensaje": "Respuesta enviada correctamente a WhatsApp"}), 200
        else:
            return jsonify({"error": f"Error en Camibot: {respuesta.status_code} - {respuesta.text}"}), 500

    except Exception as e:
        print(f"❌ Error en /enviar_respuesta: {str(e)}")
        return jsonify({"error": str(e)}), 500


# 📌 Obtener mensajes
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    try:
        conn = conectar_db()
        if conn:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM mensajes ORDER BY fecha DESC")
            mensajes = cursor.fetchall()
            conn.close()
            return jsonify(mensajes)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 📌 Actualizar estado de mensaje
@app.route("/actualizar_estado", methods=["POST"])
def actualizar_estado():
    datos = request.json
    mensaje_id = datos.get("id")
    nuevo_estado = datos.get("estado")

    if not mensaje_id or nuevo_estado not in ["Nuevo", "En proceso", "Finalizado"]:
        return jsonify({"error": "Datos incorrectos"}), 400

    conn = conectar_db()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE mensajes SET estado = %s WHERE id = %s", (nuevo_estado, mensaje_id))
            conn.commit()
        finally:
            conn.close()

    return jsonify({"mensaje": "Estado actualizado correctamente"}), 200


# Mostrar los mensajes de cada chat
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


# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    
