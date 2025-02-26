from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
import re
import time
from flujo_conversacion import FLUJO_CONVERSACION
from servicios import SERVICIOS

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

    # Validación de datos
    if not plataforma or not remitente or not mensaje:
        return jsonify({"error": "Faltan datos: plataforma, remitente o mensaje"}), 400
    
    # Manejar la conversación
    manejar_conversacion(remitente, mensaje)

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # Verificar si el lead ya existe
        cursor.execute("SELECT id, nombre FROM leads WHERE telefono = %s", (remitente,))
        lead = cursor.fetchone()

        if not lead:
            # Crear nuevo lead automáticamente
            nombre_por_defecto = f"Lead desde Chat {remitente[-10:]}"
            cursor.execute("""
                INSERT INTO leads (nombre, telefono, estado)
                VALUES (%s, %s, 'Contacto Inicial')
                ON CONFLICT (telefono) DO NOTHING
                RETURNING id
            """, (nombre_por_defecto, remitente))
            resultado = cursor.fetchone()
            if resultado:
                lead_id = resultado[0]  # Obtener el ID directamente
            else:
                # Si no se pudo crear el lead, obtener el ID del lead existente
                cursor.execute("SELECT id FROM leads WHERE telefono = %s", (remitente,))
                lead_id = cursor.fetchone()[0]
        else:
            lead_id = lead[0]  # Obtener el ID del lead existente

        # Guardar mensaje en la tabla "mensajes"
        cursor.execute("""
            INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo)
            VALUES (%s, %s, %s, 'Nuevo', 'recibido')
        """, (plataforma, remitente, mensaje))
        conn.commit()

        # Emitir eventos para actualizar la interfaz
        socketio.emit("nuevo_mensaje", {
            "plataforma": plataforma,
            "remitente": remitente,
            "mensaje": mensaje,
            "tipo": "recibido"
        })

        if lead_id:
            socketio.emit("nuevo_lead", {
                "id": lead_id,  # Usar lead_id directamente
                "nombre": nombre_por_defecto if not lead else lead[1],
                "telefono": remitente,
                "estado": "Contacto Inicial"
            })

        return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200

    except Exception as e:
        print(f"❌ Error en /recibir_mensaje: {str(e)}")
        return jsonify({"error": "Error interno del servidor"}), 500

    finally:
        liberar_db(conn)
        
        
# Funciones auxiliares
# 📌 Manejar las conversaciones automaticas
def manejar_conversacion(remitente, mensaje):
    conn = conectar_db()
    if not conn:
        return

    try:
        cursor = conn.cursor()

        # Obtener el estado actual del lead
        cursor.execute("SELECT estado, estado FROM leads WHERE telefono = %s", (remitente,))
        resultado = cursor.fetchone()
        
        if not resultado:
            print(f"❌ Lead no encontrado para el teléfono: {remitente}")
            # Crear un nuevo lead si no existe
            cursor.execute("""
                INSERT INTO leads (telefono, estado)
                VALUES (%s, 'Contacto Inicial')
                RETURNING estado
            """, (remitente,))
            resultado = cursor.fetchone()
            conn.commit()

        estado_actual = resultado[0]  # Obtener el estado actual del lead
        
        # Buscar la respuesta correspondiente en el flujo de conversación
        flujo = FLUJO_CONVERSACION.get(estado_actual, {})
        respuestas = flujo.get("respuestas", {})

        respuesta_automatica = None
        siguiente_estado = None

        # Buscar coincidencias en las respuestas
        for palabra_clave, datos_respuesta in respuestas.items():
            if palabra_clave in mensaje.lower():
                respuesta_automatica = datos_respuesta["mensaje"]
                siguiente_estado = datos_respuesta["siguiente_estado"]
                break

        # Si no se encontró una respuesta, usar una genérica
        if not respuesta_automatica:
            respuesta_automatica = "Disculpa, no entendí tu mensaje. ¿Puedes ser más específico?"

        # Enviar la respuesta automática
        enviar_mensaje_automatico(remitente, respuesta_automatica)

        # Actualizar el estado de la conversación en la base de datos
        if siguiente_estado:
            cursor.execute("UPDATE leads SET estado = %s WHERE telefono = %s", (siguiente_estado, remitente))
            conn.commit()

            # Mover el lead a la columna correspondiente
            if siguiente_estado == "servicios_solicitados":
                actualizar_estado_lead(remitente, "Seguimiento")
            elif siguiente_estado == "confirmar_reserva":
                actualizar_estado_lead(remitente, "Cliente")
            elif siguiente_estado == "fecha_evento" and not verificar_disponibilidad(servicio, fecha_evento):
                actualizar_estado_lead(remitente, "No Cliente")

        # Manejar el estado "servicios_solicitados"
        if estado_actual == "servicios_solicitados":
            servicios_solicitados = extraer_servicios(mensaje)  # Extraer servicios del mensaje
            cotizacion = generar_cotizacion(servicios_solicitados)

            # Construir el mensaje de respuesta
            respuesta = "Aquí tienes una cotización:\n"
            for detalle in cotizacion["detalles"]:
                respuesta += f"- {detalle['servicio']}: {detalle['descripcion']} (${detalle['precio']})\n"
            respuesta += f"Total: ${cotizacion['total']}\n\n"
            respuesta += "¿Puedes decirme la fecha de tu evento para revisar disponibilidad?"

            # Enviar la cotización como mensaje de texto
            enviar_mensaje_automatico(remitente, respuesta)

            # Enviar multimedia según los servicios solicitados
            if "cabina de fotos" in servicios_solicitados:
                enviar_multimedia(
                    remitente,
                    tipo="imagen",
                    url="https://tuservidor.com/imagenes/cabina.jpg",
                    mensaje="Aquí tienes una imagen de nuestra cabina de fotos:"
                )

            if "letras gigantes" in servicios_solicitados:
                enviar_multimedia(
                    remitente,
                    tipo="video",
                    url="https://tuservidor.com/videos/letras.mp4",
                    mensaje="Mira este video de nuestras letras gigantes iluminadas:"
                )

            # Actualizar el estado de la conversación
            siguiente_estado = "fecha_evento"

    except Exception as e:
        print(f"❌ Error en manejar_conversacion: {str(e)}")
    finally:
        liberar_db(conn)

# 📌 Enviar mensaje automatico                
def enviar_mensaje_automatico(remitente, mensaje):
    payload = {"telefono": remitente, "mensaje": mensaje}
    try:
        # Enviar el mensaje a través de la API de Camibot
        response = requests.post(CAMIBOT_API_URL, json=payload, timeout=5)
        
        if response.status_code == 200:
            # Emitir el evento "nuevo_mensaje" para actualizar el frontend
            socketio.emit("nuevo_mensaje", {
                "remitente": remitente,
                "mensaje": mensaje,
                "tipo": "enviado"  # Tipo "enviado" porque el CRM lo envía
            })
            print(f"📤 Emitiendo mensaje automático: {mensaje} para {remitente}")
        else:
            print(f"⚠️ Error al enviar mensaje automático: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error al enviar mensaje automático: {str(e)}")
    
                

# 📌 Actualiza el estado de un lead automáticamente durante el flujo de conversación.
def actualizar_estado_lead(remitente, nuevo_estado):
    conn = conectar_db()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE leads SET estado = %s WHERE telefono = %s", (nuevo_estado, remitente))
        conn.commit()
    except Exception as e:
        print(f"❌ Error al actualizar estado del lead: {str(e)}")
    finally:
        liberar_db(conn)


# 📌 Verificar disponibilidad
def verificar_disponibilidad(servicio, fecha_evento):
    conn = conectar_db()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) 
            FROM reservas 
            WHERE servicio = %s AND fecha_evento = %s AND estado != 'cancelada'
        """, (servicio, fecha_evento))
        count = cursor.fetchone()[0]
        return count == 0  # Disponible si no hay reservas para esa fecha
    except Exception as e:
        print(f"❌ Error al verificar disponibilidad: {str(e)}")
        return False
    finally:
        liberar_db(conn)

# 📌 Generar la cotizacion automatica     
def generar_cotizacion(servicios_solicitados):
    total = 0
    detalles = []

    for servicio in servicios_solicitados:
        if servicio in SERVICIOS:
            detalles.append({
                "servicio": servicio,
                "descripcion": SERVICIOS[servicio]["descripcion"],
                "precio": SERVICIOS[servicio]["precio"]
            })
            total += SERVICIOS[servicio]["precio"]
        else:
            detalles.append({
                "servicio": servicio,
                "descripcion": "Servicio no encontrado",
                "precio": 0
            })

    return {
        "detalles": detalles,
        "total": total
    }

# 📌 Extraer servicios
def extraer_servicios(mensaje):
    servicios_solicitados = []
    mensaje = mensaje.lower()

    for servicio in SERVICIOS:
        if servicio in mensaje:
            servicios_solicitados.append(servicio)

    return servicios_solicitados 

# 📌 Enviar archivos multimedia
def enviar_multimedia(remitente, tipo, url, mensaje=None):
    """
    Envía un archivo multimedia (imagen o video) a través de la API de WhatsApp.
    
    :param remitente: Número de teléfono del destinatario.
    :param tipo: Tipo de multimedia ("imagen" o "video").
    :param url: URL del archivo multimedia.
    :param mensaje: Mensaje opcional que acompaña al archivo.
    """
    payload = {
        "telefono": remitente,
        "mensaje": mensaje if mensaje else "Aquí tienes más información:",
        "multimedia": {
            "tipo": tipo,
            "url": url
        }
    }
    try:
        response = requests.post(CAMIBOT_API_URL, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ Error al enviar {tipo}: {response.json().get('error')}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Error de conexión al enviar {tipo}: {str(e)}")
        

# 📌 Enviar respuesta a Camibot con reintento automático
CAMIBOT_API_URL = "https://cami-bot-7d4110f9197c.herokuapp.com/enviar_mensaje"

# 📌 Endpoint para enviar o responder mensajes al whatsapp
@app.route("/enviar_mensaje", methods=["POST"])
def enviar_mensaje():
    datos = request.json
    telefono = datos.get("telefono")
    mensaje = datos.get("mensaje")

    if not telefono or not mensaje:
        return jsonify({"error": "Número de teléfono y mensaje son obligatorios"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

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
        max_intentos = 3
        for intento in range(max_intentos):
            try:
                respuesta = requests.post(CAMIBOT_API_URL, json=payload, timeout=5)
                if respuesta.status_code == 200:
                    break
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Intento {intento + 1} fallido: {str(e)}")
                time.sleep(2)  # Esperar 2 segundos antes de reintentar

        # Emitir evento para actualizar la interfaz (solo mensaje enviado)
        socketio.emit("nuevo_mensaje", {
            "remitente": telefono,
            "mensaje": mensaje,
            "tipo": "enviado"  # 🔹 Solo emitir como mensaje enviado
        })

        return jsonify({"mensaje": "Mensaje enviado correctamente"}), 200

    except Exception as e:
        print(f"❌ Error en /enviar_mensaje: {str(e)}")
        return jsonify({"error": "Error interno del servidor"}), 500

    finally:
        liberar_db(conn)


# 📌 Validación de teléfono (debe tener 13 dígitos)
def validar_telefono(telefono):
    return len(telefono) == 13 and telefono.startswith("521")


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

        # Validación de datos
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
            socketio.emit("nuevo_lead", nuevo_lead)  # 🔹 Enviar nuevo lead en tiempo real
            return jsonify({"mensaje": "Lead creado correctamente", "lead": nuevo_lead}), 200
        else:
            return jsonify({"mensaje": "No se pudo obtener el ID del lead"}), 500

    except Exception as e:
        print(f"❌ Error en /crear_lead: {str(e)}")
        return jsonify({"error": "Error interno del servidor"}), 500

    finally:
        liberar_db(conn)





# 📌 Endpoint para actualizar el estado de un lead en la base de datos
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

# 📌 Actualiza el estado de un mensaje en la base de datos.
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

#obtener los mensajes de un remitente específico Devuelve los mensajes en el formato esperado por el frontend.
# Mostrar los mensajes de cada chat
@app.route("/mensajes_chat", methods=["GET"])
def obtener_mensajes_chat():
    remitente = request.args.get("id")
    if not remitente:
        return jsonify({"error": "Falta el ID del remitente"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Obtener el nombre del lead
        cursor.execute("SELECT nombre FROM leads WHERE telefono = %s", (remitente,))
        lead = cursor.fetchone()
        nombre_lead = lead["nombre"] if lead else remitente  # Usar el teléfono si no hay nombre

        # Obtener los mensajes del remitente
        cursor.execute("SELECT * FROM mensajes WHERE remitente = %s ORDER BY fecha ASC", (remitente,))
        mensajes = cursor.fetchall()

        return jsonify({
            "nombre": nombre_lead,
            "mensajes": mensajes 
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)     

# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)