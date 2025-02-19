from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
import re
import time

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
                # 🔹 Si no tiene nombre, asignar "Lead desde Chat"
                nombre_por_defecto = f"Lead {remitente[-4:]}"  # Usa los últimos 4 dígitos del teléfono
                cursor.execute("""
                    INSERT INTO leads (nombre, telefono, estado)
                    VALUES (%s, %s, 'Contacto Inicial')
                    ON CONFLICT (telefono) DO NOTHING
                    RETURNING id
                """, (nombre_por_defecto, remitente))  

                lead_id = cursor.fetchone()
                conn.commit()
                
                if lead_id:
                    socketio.emit("nuevo_lead", {
                        "id": lead_id[0],
                        "nombre": nombre_por_defecto,
                        "telefono": remitente,
                        "estado": "Contacto Inicial"
                    })
            else:
                lead_id = lead[0]

            # Guardar mensaje en la tabla "mensajes"
            cursor.execute("INSERT INTO mensajes (plataforma, remitente, mensaje, estado) VALUES (%s, %s, %s, 'Nuevo')",
                           (plataforma, remitente, mensaje))
            conn.commit()
        finally:
            liberar_db(conn)
    
    socketio.emit("nuevo_mensaje", {"plataforma": plataforma, "remitente": remitente, "mensaje": mensaje})
    return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200



# 📌 Validación de teléfono (solo 10 dígitos numéricos)
def validar_telefono(telefono):
    return re.fullmatch(r"\d{10}", telefono) is not None


# 📌 Ruta para obtener Leads        
@app.route("/leads", methods=["GET"])
def obtener_leads():
    conn = conectar_db()
    if not conn:
        return jsonify([])

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM leads ORDER BY estado")
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
            return jsonify({"error": "Datos inválidos. El teléfono debe tener 10 dígitos."}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "No se pudo conectar a la base de datos."}), 500

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leads (nombre, telefono, estado, notas)
            VALUES (%s, %s, 'Contacto Inicial', %s)
            ON CONFLICT (telefono) DO UPDATE
            SET notas = EXCLUDED.notas  -- 🔹 Ahora actualiza las notas si el lead ya existía
            RETURNING id
        """, (nombre, telefono, notas))

        lead_id = cursor.fetchone()
        conn.commit()

        if lead_id:
            socketio.emit("nuevo_lead", {
                "id": lead_id[0],
                "nombre": nombre,
                "telefono": telefono,
                "estado": "Contacto Inicial",
                "notas": notas  # 🔹 Enviar notas al frontend en tiempo real
            })
            return jsonify({"mensaje": "Lead creado o actualizado correctamente"}), 200
        else:
            return jsonify({"mensaje": "No se pudo obtener el ID del lead"}), 500

    except Exception as e:
        return jsonify({"error": f"Error interno del servidor: {str(e)}"}), 500
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



@app.route('/editar_lead', methods=['POST'])
def editar_lead():
    data = request.get_json()

    print("📌 Datos recibidos en /editar_lead:", data)  # Debug

    lead_id = data.get("id")
    nuevo_nombre = data.get("nombre").strip() if data.get("nombre") else None
    nuevo_telefono = data.get("telefono").strip() if data.get("telefono") else None
    nuevas_notas = data.get("notas").strip() if data.get("notas") else ""

    if not lead_id or not nuevo_telefono:
        print("❌ Error: ID o teléfono faltante")
        return jsonify({"error": "ID y teléfono son obligatorios"}), 400

    if not validar_telefono(nuevo_telefono):
        print("❌ Error: Teléfono inválido")
        return jsonify({"error": "Teléfono inválido"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE leads
            SET nombre = COALESCE(%s, nombre), 
                telefono = %s, 
                notas = %s
            WHERE id = %s
        """, (nuevo_nombre, nuevo_telefono, nuevas_notas, lead_id))
        conn.commit()

        print("✅ Lead actualizado correctamente")
        return jsonify({"mensaje": "Lead actualizado correctamente"}), 200
    except Exception as e:
        print(f"❌ Error en /editar_lead: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


# 📌 Enviar respuesta a Camibot con reintento automático
CAMIBOT_API_URL = "https://cami-bot-7d4110f9197c.herokuapp.com/enviar_mensaje"

@app.route("/enviar_respuesta", methods=["POST"])
def enviar_respuesta():
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
            cursor.execute("""
                INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo)
                VALUES (%s, %s, %s, 'Nuevo', 'enviado')
            """, ("CRM", remitente, mensaje))
            conn.commit()
        finally:
            liberar_db(conn)

    payload = {"telefono": remitente, "mensaje": mensaje}

    for intento in range(3):
        respuesta = requests.post(CAMIBOT_API_URL, json=payload)
        if respuesta.status_code == 200:
            return jsonify({"mensaje": "Mensaje enviado a WhatsApp"}), 200
        print(f"⚠️ Reintentando... ({intento+1}/3)")
        time.sleep(2)

    return jsonify({"error": "No se pudo enviar el mensaje después de 3 intentos"}), 500


# 📌 Obtener mensajes
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    conn = conectar_db()
    if conn:
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM mensajes ORDER BY fecha DESC")
            mensajes = cursor.fetchall()
            return jsonify(mensajes)
        finally:
            liberar_db(conn)

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
        finally:
            liberar_db(conn)

# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    
