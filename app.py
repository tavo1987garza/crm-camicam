from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import sqlite3
import datetime
from flask_cors import CORS  # Permite solicitudes desde el frontend

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")  # 🔹 Forzar modo gevent
CORS(app)  # Habilitar CORS para conectar con un frontend externo

# 📌 Ruta raiz 
@app.route("/")
def home():
    return "¡CRM de Camicam funcionando!"

# 📌 Función para conectar a la base de datos con autocommit
def conectar_db():
    conn = sqlite3.connect("crm_camicam.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Permite acceder a las columnas por nombre
    return conn

# 📌 Endpoint para recibir mensajes y emitir notificación en tiempo real
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
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

    # 🔹 Emitir evento de nuevo mensaje
    socketio.emit("nuevo_mensaje", {
        "plataforma": plataforma,
        "remitente": remitente,
        "mensaje": mensaje
    }, broadcast=True)

    return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200


# 📌 Endpoint para consultar los mensajes (con filtro opcional por estado)
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    estado = request.args.get("estado")  # Permite filtrar por estado
    conn = conectar_db()
    cursor = conn.cursor()

    if estado:
        cursor.execute("SELECT * FROM mensajes WHERE estado = ? ORDER BY fecha DESC", (estado,))
    else:
        cursor.execute("SELECT * FROM mensajes ORDER BY fecha DESC")

    mensajes = cursor.fetchall()
    conn.close()

    mensajes_json = [
        {"id": msg["id"], "plataforma": msg["plataforma"], "remitente": msg["remitente"],
         "mensaje": msg["mensaje"], "estado": msg["estado"], "fecha": msg["fecha"]}
        for msg in mensajes
    ]

    return jsonify(mensajes_json)

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
