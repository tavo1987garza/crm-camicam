from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import sqlite3

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # 🔹 Eliminamos async_mode

# 📌 Ruta raiz 
@app.route("/")
def home():
    return "¡CRM de Camicam funcionando!"

# 📌 Función para conectar a la base de datos
def conectar_db():
    conn = sqlite3.connect("crm_camicam.db")
    conn.row_factory = sqlite3.Row
    return conn

# 📌 Endpoint para recibir mensajes y emitir notificación en tiempo real
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    try:
        datos = request.json
        plataforma = datos.get("plataforma")
        remitente = datos.get("remitente")
        mensaje = datos.get("mensaje")

        if not plataforma or not remitente or not mensaje:
            return jsonify({"error": "Faltan datos"}), 400

        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO mensajes (plataforma, remitente, mensaje, estado) VALUES (?, ?, ?, 'Nuevo')",
                       (plataforma, remitente, mensaje))
        conn.commit()
        conn.close()

        socketio.emit("nuevo_mensaje", {"plataforma": plataforma, "remitente": remitente, "mensaje": mensaje})

        return jsonify({"mensaje": "Mensaje recibido correctamente"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 📌 Endpoint para contestar al cliente
@app.route("/enviar_respuesta", methods=["POST"])
def enviar_respuesta():
    try:
        datos = request.json
        print("📥 Datos recibidos:", datos)  # 🔹 Agrega este log para ver qué está recibiendo

        remitente = datos.get("remitente")
        mensaje = datos.get("mensaje")

        if not remitente or not mensaje:
            print("⚠️ Faltan datos en la solicitud")  # 🔹 Log adicional
            return jsonify({"error": "Faltan datos"}), 400

        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO mensajes (plataforma, remitente, mensaje, estado) VALUES (?, ?, ?, 'Enviado')",
                       ("CRM", remitente, mensaje))
        conn.commit()
        conn.close()

        socketio.emit("respuesta_mensaje", {"remitente": remitente, "mensaje": mensaje})
        print("✅ Mensaje enviado correctamente")

        return jsonify({"mensaje": "Respuesta enviada correctamente"}), 200

    except Exception as e:
        print("❌ Error en /enviar_respuesta:", str(e))  # 🔹 Agrega esto para ver el error exacto
        return jsonify({"error": str(e)}), 500




# 📌 Endpoint para consultar los mensajes (con filtro opcional por estado)
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    remitente = request.args.get("remitente")
    conn = conectar_db()
    cursor = conn.cursor()

    if remitente:
        cursor.execute("SELECT * FROM mensajes WHERE remitente = ? ORDER BY fecha DESC", (remitente,))
    else:
        cursor.execute("SELECT * FROM mensajes ORDER BY fecha DESC")

    mensajes = cursor.fetchall()
    conn.close()

    # Si no hay mensajes, devolver una lista vacía en JSON
    return jsonify([dict(msg) for msg in mensajes]) if mensajes else jsonify([])


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
    return render_template("index.html")  # Flask busca este archivo en `templates/`

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
