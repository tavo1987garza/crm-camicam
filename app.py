from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
import time
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# 📌 Ruta raíz
@app.route("/")
def home():
    return "¡CRM de Camicam funcionando!"

# 📌 Configuración de la conexión con *connection pooling*
DATABASE_URL = os.environ.get("DATABASE_URL", "")

try:
    db_pool = pool.SimpleConnectionPool(1, 10, dsn=DATABASE_URL, sslmode="require")
except Exception as e:
    print("❌ Error al conectar con la base de datos:", str(e))
    db_pool = None

def conectar_db():
    if db_pool is None:
        print("❌ No se pudo iniciar el pool de conexiones")
        return None
    try:
        return db_pool.getconn()
    except Exception as e:
        print("❌ Error al obtener conexión del pool:", str(e))
        return None

def liberar_db(conn):
    if conn:
        db_pool.putconn(conn)

# 📌 Validación de teléfono (debe tener 13 dígitos)
def validar_telefono(telefono):
    return len(telefono) == 13 and telefono.startswith("521")

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
            
            # Verificar si el lead ya existe
            cursor.execute("SELECT id, nombre FROM leads WHERE telefono = %s", (remitente,))
            lead = cursor.fetchone()
            
            if not lead:
                # Crear nuevo lead
                nombre_por_defecto = f"Lead desde Chat {remitente[-10:]}"
                cursor.execute("""
                    INSERT INTO leads (nombre, telefono, estado)
                    VALUES (%s, %s, 'Contacto Inicial')
                    ON CONFLICT (telefono) DO NOTHING
                    RETURNING id
                """, (nombre_por_defecto, remitente))
                lead_id = cursor.fetchone()
            else:
                lead_id = lead[0]

            # Guardar mensaje en la tabla "mensajes"
            cursor.execute("""
                INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo)
                VALUES (%s, %s, %s, 'Nuevo', 'recibido')
            """, (plataforma, remitente, mensaje))
            conn.commit()

            # Emitir eventos para actualizar la interfaz
            socketio.emit("nuevo_mensaje", {"plataforma": plataforma, "remitente": remitente, "mensaje": mensaje, "tipo": "recibido"})
            if lead_id:
                socketio.emit("nuevo_lead", {
                    "id": lead_id[0],
                    "nombre": nombre_por_defecto if not lead else lead[1],
                    "telefono": remitente,
                    "estado": "Contacto Inicial"
                })
        finally:
            liberar_db(conn)
    
    return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200

# 📌 Ruta para obtener Leads        
@app.route("/leads", methods=["GET"])
def obtener_leads():
    conn = conectar_db()
    if not conn:
        return jsonify([])

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT l.*, 
                   (SELECT mensaje FROM mensajes WHERE remitente = l.telefono ORDER BY fecha DESC LIMIT 1) as ultimo_mensaje
            FROM leads l
            ORDER BY l.estado
        """)
        leads = cursor.fetchall()
        return jsonify(leads if leads else [])
    except Exception as e:
        print("❌ Error en /leads:", str(e))
        return jsonify([])
    finally:
        liberar_db(conn)

# 📌 Crear un nuevo lead manualmente
@app.route("/crear_lead", methods=["POST"])
def crear_lead():
    try:
        datos = request.json
        nombre = datos.get("nombre")
        telefono = datos.get("telefono")
        notas = datos.get("notas", "")

        if not nombre or not telefono or not validar_telefono(telefono):
            return jsonify({"error": "El teléfono debe tener 13 dígitos (ejemplo: 521XXXXXXXXXX)."}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "No se pudo conectar a la base de datos."}), 500

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leads (nombre, telefono, estado, notas)
            VALUES (%s, %s, 'Contacto Inicial', %s)
            ON CONFLICT (telefono) DO UPDATE
            SET notas = EXCLUDED.notas
            RETURNING id
        """, (nombre, telefono, notas))

        lead_id = cursor.fetchone()
        conn.commit()

        if lead_id:
            nuevo_lead = {
                "id": lead_id[0],
                "nombre": nombre,
                "telefono": telefono,
                "estado": "Contacto Inicial",
                "notas": notas
            }
            socketio.emit("nuevo_lead", nuevo_lead)
            return jsonify({"mensaje": "Lead creado correctamente", "lead": nuevo_lead}), 200
        else:
            return jsonify({"mensaje": "No se pudo obtener el ID del lead"}), 500

    except Exception as e:
        return jsonify({"error": f"Error en /crear_lead: {str(e)}"}), 500
    finally:
        liberar_db(conn)

# 📌 Endpoint para enviar mensajes desde el chat o desde leads
@app.route("/enviar_mensaje", methods=["POST"])
def enviar_mensaje():
    datos = request.json
    telefono = datos.get("telefono")
    mensaje = datos.get("mensaje")

    if not telefono or not mensaje:
        return jsonify({"error": "Número de teléfono y mensaje son obligatorios"}), 400

    conn = conectar_db()
    if conn:
        try:
            cursor = conn.cursor()
            # Guardar mensaje en la base de datos
            cursor.execute("""
                INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo)
                VALUES (%s, %s, %s, 'Nuevo', 'enviado')
            """, ("CRM", telefono, mensaje))
            conn.commit()

            # Enviar mensaje a través de la API de Camibot
            payload = {"telefono": telefono, "mensaje": mensaje}
            for intento in range(3):  # Reintentar hasta 3 veces
                respuesta = requests.post(CAMIBOT_API_URL, json=payload)
                if respuesta.status_code == 200:
                    break
                time.sleep(2)  # Esperar 2 segundos antes de reintentar

            # Emitir evento para actualizar la interfaz
            nuevo_mensaje = {"remitente": telefono, "mensaje": mensaje, "tipo": "enviado"}
            socketio.emit("nuevo_mensaje", nuevo_mensaje)

            return jsonify({"mensaje": "Mensaje enviado correctamente"}), 200
        except Exception as e:
            return jsonify({"error": f"Error al enviar mensaje: {str(e)}"}), 500
        finally:
            liberar_db(conn)

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
        telefono = datos.get("telefono")  # Necesario para borrar sus mensajes

        if not lead_id or not telefono:
            return jsonify({"error": "Faltan datos"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

        cursor = conn.cursor()

        # 🔹 Eliminar todos los mensajes asociados al teléfono del lead
        cursor.execute("DELETE FROM mensajes WHERE remitente = %s", (telefono,))

        # 🔹 Eliminar el lead de la tabla leads
        cursor.execute("DELETE FROM leads WHERE id = %s", (lead_id,))

        conn.commit()
        conn.close()

        # 🔹 Notificar al frontend para actualizar la interfaz
        socketio.emit("lead_eliminado", {"id": lead_id, "telefono": telefono})

        return jsonify({"mensaje": "Lead y sus mensajes eliminados correctamente"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 📌 Obtener mensajes de un remitente específico
@app.route("/mensajes_chat", methods=["GET"])
def obtener_mensajes_chat():
    remitente = request.args.get("id")
    if not remitente:
        return jsonify({"error": "Falta el ID del remitente"}), 400

    conn = conectar_db()
    if conn:
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM mensajes WHERE remitente = %s ORDER BY fecha ASC", (remitente,))
            mensajes = cursor.fetchall()
            return jsonify(mensajes)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            liberar_db(conn)

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)