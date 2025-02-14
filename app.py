from flask import Flask, request, jsonify, render_template
import sqlite3
import datetime
from flask_cors import CORS  # Permite solicitudes desde el frontend

app = Flask(__name__)
CORS(app)  # Habilitar CORS para conectar con un frontend externo

# 📌 Función para conectar a la base de datos con autocommit
def conectar_db():
    conn = sqlite3.connect("crm_camicam.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Permite acceder a las columnas por nombre
    return conn

# 📌 Endpoint para recibir mensajes desde Camibot
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    try:
        datos = request.get_json(force=True)  # 👈 Esto forzará la conversión a JSON
        print(f"📩 Datos recibidos en Flask: {datos}")  # 👈 Ver qué está llegando

        plataforma = datos.get("plataforma")
        remitente = datos.get("remitente")
        mensaje = datos.get("mensaje")

        if not plataforma or not remitente or not mensaje:
            print("⚠️ Faltan datos en la petición")  # 👈 Mensaje para depurar
            return jsonify({"error": "Faltan datos"}), 400

        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO mensajes (plataforma, remitente, mensaje, estado, fecha) VALUES (?, ?, ?, 'Nuevo', CURRENT_TIMESTAMP)",
            (plataforma, remitente, mensaje)
        )
        conn.commit()
        conn.close()

        return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200
    except Exception as e:
        print(f"❌ ERROR en /recibir_mensaje: {e}")  # 👈 Imprimir errores
        return jsonify({"error": str(e)}), 500


# 📌 Endpoint para consultar los mensajes almacenados
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    try:
        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, plataforma, remitente, mensaje, estado, fecha FROM mensajes ORDER BY fecha DESC")
        mensajes = cursor.fetchall()
        
        mensajes_json = [dict(msg) for msg in mensajes]  # Convertimos a JSON
        return jsonify(mensajes_json)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# 📌 Endpoint para actualizar el estado de un mensaje
@app.route("/actualizar_estado", methods=["POST"])
def actualizar_estado():
    datos = request.json
    mensaje_id = datos.get("id")
    nuevo_estado = datos.get("estado")

    if not mensaje_id or nuevo_estado not in ["Nuevo", "En proceso", "Finalizado"]:
        return jsonify({"error": "Datos incorrectos"}), 400

    try:
        conn = conectar_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE mensajes SET estado = ? WHERE id = ?", (nuevo_estado, mensaje_id))
        conn.commit()
        return jsonify({"mensaje": "Estado actualizado correctamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")  # Flask busca este archivo en `templates/`

if __name__ == "__main__":
    app.run(debug=True)
