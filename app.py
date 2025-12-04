
from dotenv import load_dotenv
import os
import json
import re
import time
import base64
import uuid 
from datetime import datetime, timezone, date
import requests
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from flask import (
    Flask, request, jsonify, render_template, send_from_directory,
    current_app, g, abort
)
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

from functools import wraps

from werkzeug.security import generate_password_hash

load_dotenv()

# 📌 Ruta raíz
@app.route("/") 
def home():
    return "¡CRM de Camicam funcionando!"



# 📌 Configuración de la URL de la base de datos
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    app.logger.critical("Falta configurar DATABASE_URL en las variables de entorno")
    raise RuntimeError("Falta configurar DATABASE_URL")

# 📌 Inicializar el pool de conexiones
try:
    db_pool = pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        sslmode="require"
    )
    app.logger.info("Pool de conexiones a la base de datos iniciado con éxito")
except Exception as e:
    app.logger.error(f"Error al inicializar el pool de conexiones: {e}")
    db_pool = None

def conectar_db():
    """Obtiene una conexión del pool."""
    if db_pool is None:
        app.logger.error("Intento de conectar sin pool inicializado")
        return None
    try:
        return db_pool.getconn()
    except Exception as e:
        app.logger.error(f"Error al obtener conexión del pool: {e}")
        return None

def liberar_db(conn):
    """Devuelve la conexión al pool."""
    if not conn or db_pool is None:
        return
    try:
        db_pool.putconn(conn)
    except Exception as e:
        app.logger.error(f"Error al liberar conexión al pool: {e}")






        
        
        
##################################
#----------SECCION PANEL----------
##################################   

# 📌 Endpoint para el buscador de fecha
@app.route("/calendario/checar_fecha")
def checar_fecha():
    fecha = request.args.get("fecha")
    conn = conectar_db()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT COUNT(*)
            FROM calendario
            WHERE DATE(fecha AT TIME ZONE 'UTC') = %s
        """, (fecha,))
        cnt = cur.fetchone()[0]
    finally:
        liberar_db(conn)
    return jsonify({"count": cnt})



# 📌 Endpoint para la visualzacion de Próximos eventos        
@app.route("/calendario/proximos")
def proximos_eventos():
    try:
        lim = int(request.args.get("limite", 5))
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB pool no inicializado")
        cur = conn.cursor()
        cur.execute("""
          SELECT id,
                 TO_CHAR(fecha AT TIME ZONE 'UTC','YYYY-MM-DD'),
                 COALESCE(titulo,''),
                 COALESCE(servicios::text,'{}')
          FROM calendario
          WHERE fecha AT TIME ZONE 'UTC' >= %s
          ORDER BY fecha ASC
          LIMIT %s
        """, (date.today(), lim))
        rows = cur.fetchall()
        liberar_db(conn)

        out = []
        for id_, fecha, titulo, servicios_text in rows:
            try:
                servicios = json.loads(servicios_text)
            except:
                servicios = {}
                current_app.logger.warning(f"Servicios JSON inválido: {servicios_text}")
            out.append({"id": id_, "fecha": fecha, "titulo": titulo, "servicios": servicios})
        return jsonify(out), 200

    except Exception:
        current_app.logger.exception("Error en /calendario/proximos")
        return jsonify([]), 200



# 📌 Endpoint para mostras los Ultimos Leads
@app.route("/leads/ultimos")
def ultimos_leads():
    try:
        lim = int(request.args.get("limite", 3))
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB pool no inicializado")
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, nombre, telefono FROM leads ORDER BY id DESC LIMIT %s", (lim,))
        rows = cur.fetchall()
        liberar_db(conn)
        return jsonify(rows), 200

    except Exception:
        current_app.logger.exception("Error en /leads/ultimos")
        return jsonify([]), 200


# 📌 Endpoint para mostrar el KPI ensual (La meta mensual)
@app.route("/reportes/kpi_mes")
def kpi_mes():
    try:
        hoy = datetime.utcnow()
        mes, anio = hoy.month, hoy.year

        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB pool no inicializado")
        cur = conn.cursor()
        cur.execute("""
          SELECT COUNT(*) FROM calendario
           WHERE EXTRACT(YEAR FROM fecha AT TIME ZONE 'UTC')=%s
             AND EXTRACT(MONTH FROM fecha AT TIME ZONE 'UTC')=%s
        """, (anio, mes))
        actual = cur.fetchone()[0]

        cur.execute("SELECT valor FROM config WHERE clave='meta_mensual'")
        row = cur.fetchone()
        meta = int(row[0]) if row and row[0].isdigit() else 15

        liberar_db(conn)
        return jsonify({"actual": actual, "meta": meta}), 200

    except Exception:
        current_app.logger.exception("Error en /reportes/kpi_mes")
        # Fallback: actual = 0, meta = 15
        return jsonify({"actual": 0, "meta": 15}), 200

   

##################################
#----------SECCION LEADS---------- 
##################################   

# 📌 NUEVO: Guardar contexto del bot
@app.route("/leads/context", methods=["POST"])
def guardar_contexto_lead():
    conn = None
    try:
        datos = request.json
        telefono = datos.get("telefono")
        contexto = datos.get("context")
        
        if not telefono or not contexto:
            return jsonify({"error": "Faltan datos: telefono o context"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión a BD"}), 500

        cursor = conn.cursor()
        
        # Crear o actualizar contexto
        cursor.execute("""
            INSERT INTO leads (telefono, nombre, estado, contexto, last_activity) 
            VALUES (%s, %s, 'Contacto Inicial', %s, NOW())
            ON CONFLICT (telefono) 
            DO UPDATE SET 
                contexto = EXCLUDED.contexto, 
                last_activity = NOW(),
                estado = CASE 
                    WHEN leads.estado = 'Finalizado' THEN 'Contacto Inicial' 
                    ELSE leads.estado 
                END
            RETURNING id
        """, (telefono, f"Lead {telefono[-4:]}", json.dumps(contexto)))
        
        conn.commit()
        return jsonify({"mensaje": "Contexto guardado"}), 200

    except Exception as e:
        print(f"❌ Error en /leads/context: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        if conn:
            liberar_db(conn)
            
@app.route("/leads/context", methods=["GET"])   
def obtener_contexto_lead():
    telefono = request.args.get("telefono")
    if not telefono:
        return jsonify({"error": "Falta teléfono"}), 400
    conn = conectar_db()
    if not conn:
        return jsonify({"context": None}), 200
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT contexto FROM leads WHERE telefono = %s", (telefono,))
        row = cursor.fetchone()
        if not row or not row['contexto']:
            return jsonify({"context": None}), 200
        
        contexto_raw = row['contexto']
        
        # Caso 1: ya es un dict (por columna JSONB en PostgreSQL)
        if isinstance(contexto_raw, dict):
            contexto = contexto_raw
        # Caso 2: es una cadena JSON (por columna TEXT en PostgreSQL)
        else:
            try:
                contexto = json.loads(contexto_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                app.logger.warning(f"Contexto malformado para {telefono}: {contexto_raw}")
                return jsonify({"context": None}), 200
        
        return jsonify({"context": contexto}), 200
    except Exception as e:
        app.logger.error(f"Error inesperado en /leads/context: {e}")
        return jsonify({"context": None}), 200
    finally:
        liberar_db(conn)
        
        
# 📌 NUEVO: Limpiar contextos antiguos (ejecutar diariamente)
@app.route("/leads/cleanup_context", methods=["POST"])
def limpiar_contextos():
    try:
        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión"}), 500

        cursor = conn.cursor()
        cursor.execute("UPDATE leads SET contexto = NULL WHERE last_activity < NOW() - INTERVAL '30 days'")
        conn.commit()
        
        return jsonify({"mensaje": "Contextos antiguos limpiados"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
# 📌 Obtener ID de lead por teléfono
@app.route("/lead_id", methods=["GET"])
def obtener_lead_id():
    telefono = request.args.get("telefono")
    if not telefono:
        return jsonify({"error": "Falta el parámetro 'telefono'"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a BD"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM leads WHERE telefono = %s", (telefono,))
        row = cursor.fetchone()
        if row:
            return jsonify({"id": row[0]}), 200
        else:
            return jsonify({"error": "Lead no encontrado"}), 404
    except Exception as e:
        print(f"❌ Error en /lead_id: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        

# 📌 Endpoint para recibir mensajes desde WhatsApp
# 📌 Endpoint para recibir mensajes desde WhatsApp
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    datos = request.json
    plataforma = datos.get("plataforma")
    remitente = str(datos.get("remitente", ""))
    mensaje = datos.get("mensaje")
    tipo = datos.get("tipo")

    if not plataforma or not remitente or mensaje is None:
        return jsonify({"error": "Faltan datos: plataforma, remitente o mensaje"}), 400

    # Ampliar tipos válidos (texto, imagen y opcional video)
    tipos_validos = {
        "enviado", "recibido",
        "recibido_imagen", "enviado_imagen",
        "recibido_video", "enviado_video"
    }
    if tipo not in tipos_validos:
        # Por compatibilidad, cualquier cosa desconocida se trata como recibido (texto)
        tipo = "recibido"

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # Verificar si el lead ya existe
        cursor.execute("SELECT id, nombre FROM leads WHERE telefono = %s", (remitente,))
        lead = cursor.fetchone()
        lead_id = None
        lead_creado = False

        if not lead:
            nombre_por_defecto = f"Lead desde Chat {remitente[-10:]}"
            cursor.execute("""
                INSERT INTO leads (nombre, telefono, estado)
                VALUES (%s, %s, 'Contacto Inicial')
                ON CONFLICT (telefono) DO NOTHING
                RETURNING id
            """, (nombre_por_defecto, remitente))
            row = cursor.fetchone()
            if row:
                lead_id = row[0]
                lead_creado = True
        else:
            lead_id = lead[0]

        # Guardar el mensaje en la tabla `mensajes`
        cursor.execute("""
            INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo)
            VALUES (%s, %s, %s, 'Nuevo', %s)
        """, (plataforma, remitente, mensaje, tipo))
        conn.commit()

        # 🔹 Procesar eventos especiales del bot para cambiar estado
        if tipo == "enviado" and isinstance(mensaje, str) and mensaje.startswith("EVENT:lead_seguimiento"):
            try:
                partes = mensaje.split(" ", 2)
                if len(partes) >= 2:
                    tipo_seguimiento = partes[1]
                    if tipo_seguimiento == "XV":
                        nuevo_estado = "Seguimiento XV"
                    elif tipo_seguimiento == "Boda":
                        nuevo_estado = "Seguimiento Boda"
                    else:
                        nuevo_estado = "Seguimiento Otro"

                    # Actualizar estado en la base de datos
                    cursor.execute("UPDATE leads SET estado = %s WHERE telefono = %s", (nuevo_estado, remitente))
                    conn.commit()

                    # Emitir evento en tiempo real al frontend
                    if lead_id:
                        socketio.emit("lead_estado_actualizado", {
                            "id": lead_id,
                            "estado_nuevo": nuevo_estado,
                            "telefono": remitente
                        })
            except Exception as e:
                print(f"⚠️ Error procesando evento de seguimiento: {str(e)}")

        # Notificar nuevo mensaje a todos los clientes conectados
        socketio.emit("nuevo_mensaje", {
            "plataforma": plataforma,
            "remitente": remitente,
            "mensaje": mensaje,
            "tipo": tipo
        })

        # Notificar SOLO si se creó un lead nuevo (evita duplicados)
        if lead_creado:
            socketio.emit("nuevo_lead", {
                "id": lead_id,
                "nombre": nombre_por_defecto,
                "telefono": remitente,
                "estado": "Contacto Inicial"
            })

        return jsonify({"mensaje": "Mensaje recibido y almacenado"}), 200

    except Exception as e:
        print(f"❌ Error en /recibir_mensaje: {str(e)}")
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        liberar_db(conn)
             

# 📌 Enviar respuesta a Camibot con reintento automático

CAMIBOT_API_URL = os.getenv("CAMIBOT_API_URL", "http://localhost:3001")

@app.route("/enviar_mensaje", methods=["POST"])
def enviar_mensaje():
    datos = request.json
    telefono = datos.get("telefono")
    tipo = datos.get("tipo", "texto")
    url_imagen = datos.get("url")
    url_video = datos.get("url_video")
    caption = datos.get("caption", "")
    mensaje_texto = datos.get("mensaje")

    if not telefono:
        return jsonify({"error": "Número de teléfono es obligatorio"}), 400

    # Imagen
    if tipo == "imagen":
        if not url_imagen:
            return jsonify({"error": "Falta la URL de la imagen"}), 400
        payload = {"telefono": telefono, "imageUrl": url_imagen, "caption": caption}
        max_intentos = 3
        for intento in range(max_intentos):
            try:
                r = requests.post(f"{CAMIBOT_API_URL}/enviar_imagen", json=payload, timeout=5)
                if r.status_code == 200:
                    break
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Intento {intento + 1} fallido (imagen): {str(e)}")
                time.sleep(2)
        return jsonify({"mensaje": "Imagen enviada correctamente"}), 200

    # ⬇️ Video
    if tipo == "video":
        if not url_video:
            return jsonify({"error": "Falta la URL del video"}), 400
        payload = {"telefono": telefono, "videoUrl": url_video, "caption": caption}
        max_intentos = 3
        for intento in range(max_intentos):
            try:
                r = requests.post(f"{CAMIBOT_API_URL}/enviar_video", json=payload, timeout=5)
                if r.status_code == 200:
                    break
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Intento {intento + 1} fallido (video): {str(e)}")
                time.sleep(2)
        return jsonify({"mensaje": "Video enviado correctamente"}), 200

    # Texto
    if not mensaje_texto:
        return jsonify({"error": "Falta el 'mensaje' de texto"}), 400

    payload = {"telefono": telefono, "mensaje": mensaje_texto}
    max_intentos = 3
    for intento in range(max_intentos):
        try:
            r = requests.post(f"{CAMIBOT_API_URL}/enviar_mensaje", json=payload, timeout=5)
            if r.status_code == 200:
                break
        except requests.exceptions.RequestException as e:
            print(f"⚠️ Intento {intento + 1} fallido (texto): {str(e)}")
            time.sleep(2)
    return jsonify({"mensaje": "Mensaje enviado correctamente"}), 200


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


# 📌 Endpoint para actualizar estado de Lead
@app.route("/cambiar_estado_lead", methods=["POST"])
def cambiar_estado_lead():
    try:
        datos = request.json
        lead_id = datos.get("id")
        nuevo_estado = datos.get("estado")
        
        # ✅ Lista COMPLETA de estados válidos (incluyendo los nuevos)
        estados_validos = [
            "Contacto Inicial",
            "En proceso",
            "Seguimiento XV",
            "Seguimiento Boda",
            "Seguimiento Otro",
            "Cliente",
            "No cliente"
        ]
        
        if not lead_id or nuevo_estado not in estados_validos:
            return jsonify({"error": "Estado no válido"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión"}), 500

        cursor = conn.cursor()

        # Obtener el teléfono del lead para el evento
        cursor.execute("SELECT telefono FROM leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
        telefono = row[0] if row else None

        # Actualizar estado
        cursor.execute("UPDATE leads SET estado = %s WHERE id = %s", (nuevo_estado, lead_id))
        conn.commit()

        # Emitir evento en tiempo real
        if telefono:
            socketio.emit("lead_estado_actualizado", {
                "id": lead_id,
                "estado_nuevo": nuevo_estado,
                "telefono": telefono
            })

        conn.close()
        return jsonify({"mensaje": "Estado actualizado correctamente"}), 200

    except Exception as e:
        print(f"❌ Error en /cambiar_estado_lead: {str(e)}")
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
    
    
    
    
#'''''''''''''''''''''''''''''''''''''''''''''''
#------------SECION DE CALENDARIO---------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
    
    
# 📌 Obtener Años con Eventos (Nuevo)
@app.route("/calendario/anios", methods=["GET"])
def obtener_anios_calendario():
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT EXTRACT(YEAR FROM fecha) as anio,
                   COUNT(*) OVER (PARTITION BY EXTRACT(YEAR FROM fecha)) as total
            FROM calendario
            ORDER BY anio DESC
        """)
        rows = cursor.fetchall()
        anios = [{"anio": int(row[0]), "total_eventos": row[1]} for row in rows]
        return jsonify({"anios": anios}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
# 📌 Crear / actualizar color para un año
@app.route("/calendario/agregar_anio", methods=["POST"])
def agregar_anio_color():
    data = request.get_json()
    anio  = data.get("anio")
    color = data.get("color")

    if not anio or not color:
        return jsonify({"error": "Faltan datos (anio/color)"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO anio_color (anio, color)
            VALUES (%s, %s)
            ON CONFLICT (anio)
            DO UPDATE SET color = EXCLUDED.color
        """, (anio, color))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

        
# 📌 Endpoint para Obtener Eventos por Año
@app.route("/calendario/agrupado_por_anios", methods=["GET"])
def calendario_agrupado():
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No hay DB"}), 500

    try:
        cursor = conn.cursor()
        
        # 1. Primero obtenemos los años disponibles
        cursor.execute("""
            SELECT DISTINCT EXTRACT(YEAR FROM fecha) as anio
            FROM calendario
            ORDER BY anio DESC
        """)
        anios = [int(row[0]) for row in cursor.fetchall()]
        
        # 2. Obtenemos todos los eventos (similar al endpoint original)
        cursor.execute("""
            SELECT id, fecha, titulo, notas, ticket, servicios
            FROM calendario
            ORDER BY fecha ASC
        """)
        
        eventos = []
        for row in cursor.fetchall():
            eventos.append({
                "id": row[0],
                "fecha": row[1].strftime("%Y-%m-%d"),
                "anio": row[1].year,
                "titulo": row[2] or "",
                "notas": row[3] or "",
                "ticket": float(row[4]) if row[4] else 0.0,
                "servicios": row[5] if row[5] else {}
            })
        
        return jsonify({
            "anios": anios,
            "eventos": eventos
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

        
# 📌 Endpoint para agregar fechas al Calendario 
@app.route("/calendario/agregar_manual", methods=["POST"])
def agregar_fecha_manual():
    data = request.json
    fecha_str = data.get("fecha")      # "YYYY-MM-DD"
    force     = data.get("force", False)  
    
    # Asegurar que la fecha se interprete en la zona horaria correcta
    try:
        # Parsear la fecha como UTC explícitamente
        fecha_utc = datetime.strptime(fecha_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        fecha_local = fecha_utc.astimezone()  # Convierte a zona horaria del servidor si es necesario
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Use YYYY-MM-DD"}), 400
    
    lead_id = data.get("lead_id")      # int o None
    titulo = data.get("titulo", "")
    notas = data.get("notas", "")

    # Nuevos campos:
    ticket = data.get("ticket", 0)             # numérico
    servicios_input = data.get("servicios")    # string con JSON o ya un dict

    if not fecha_str:
        return jsonify({"error": "Falta la fecha en formato YYYY-MM-DD"}), 400

    # Conexión a DB 
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # Convertir ticket a float o decimal
        # (Si te llega como string, lo conviertes con float(...). Podrías usar Decimal de Python.
        ticket_value = float(ticket) if ticket else 0.0

        # Manejar el JSON de servicios
        # Si el front te manda ya un objeto JSON, en Python lo recibes como dict.
        # O si te manda un string JSON, hay que parsearlo:
        import json
        if isinstance(servicios_input, str):
            try:
                servicios_json = json.loads(servicios_input)  # parsea string a dict
            except:
                servicios_json = {}
        elif isinstance(servicios_input, dict):
            servicios_json = servicios_input
        else:
            servicios_json = {}
            
          # 1️⃣  ¿Cuántos eventos hay ya ese día? solo se puede agregar hasta 4 eventos 
        cursor.execute("SELECT COUNT(*) FROM calendario WHERE fecha = %s", (fecha_str,))
        ya_hay = cursor.fetchone()[0]

        if ya_hay == 1 and not force:
            # Hay 1 evento y aún no confirmas el segundo
            return jsonify({"ok": False,
                            "second_possible": True,
                            "mensaje": f"Ya hay un evento el {fecha_str}. ¿Agregar un segundo?"}), 200

        if ya_hay == 2 and not force:
            # Hay 2 evento y aún no confirmas el tercero
            return jsonify({"ok": False,
                            "second_possible": True,
                            "mensaje": f"Ya hay 2 eventos el {fecha_str}. ¿Agregar un tercero?"}), 200
            
        if ya_hay == 3 and not force:
            # Hay 3 evento y aún no confirmas el cuarto
            return jsonify({"ok": False,
                            "second_possible": True,
                            "mensaje": f"Ya hay 3 eventos el {fecha_str}. ¿Agregar un cuarto?"}), 200
            
        if ya_hay >= 4:
            # Aqui manejamos el limite de hasta 4 eventos 
            return jsonify({"ok": False,
                            "mensaje": f"El {fecha_str} a alcanzado el limite de 4 eventos registrados."}), 200



        # Insertar en la tabla calendario
        cursor.execute("""
            INSERT INTO calendario (fecha, lead_id, titulo, notas, ticket, servicios)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """, (
            fecha_local.date(),  # Guardar solo la parte de fecha (sin zona horaria)
            lead_id,
            titulo,
            notas,
            ticket_value,
            json.dumps(servicios_json)  # serializar dict a string JSON
        ))
        conn.commit()

            
            
        # ⚡️  EMITIR EVENTO EN TIEMPO REAL
        socketio.emit(
            "calendario_actualizado",
            {
                "accion": "nueva_fecha",
                "anio": fecha_local.year,
                "fecha": fecha_str,
                "titulo": titulo
            }
        )

        return jsonify({
            "ok": True,
            "mensaje": f"Fecha {fecha_str} agregada correctamente al calendario."
        }), 200

    except Exception as e:
        print(f"❌ Error en /calendario/agregar_manual: {str(e)}")
        return jsonify({"error": str(e)}), 500

    finally:
        liberar_db(conn)


  
# 📌 Obtener todas las fechas ocupadas + colores por año
@app.route("/calendario/fechas_ocupadas", methods=["GET"])
def fechas_ocupadas():
    conn = None
    try:
        # 1) Conexión
        conn = conectar_db()
        if not conn:
            raise RuntimeError("No hay conexión a la base de datos")

        cursor = conn.cursor()

        # 2) Fechas y detalles
        cursor.execute("""
            SELECT 
                c.id,
                TO_CHAR(c.fecha AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS fecha,
                c.lead_id, 
                COALESCE(c.titulo, '')    AS titulo,
                COALESCE(c.notas, '')     AS notas,
                COALESCE(c.ticket, 0)::float AS ticket,
                c.servicios::text         AS servicios_text,
                l.nombre                  AS lead_nombre,
                EXTRACT(YEAR FROM c.fecha AT TIME ZONE 'UTC')::int AS anio
            FROM calendario c
            LEFT JOIN leads l ON c.lead_id = l.id
            ORDER BY c.fecha DESC
        """)
        filas = cursor.fetchall()

        fechas = []
        for row in filas:
            # parsear el JSON de servicios
            try:
                servicios = json.loads(row[6])
            except Exception:
                servicios = {}
                app.logger.warning(f"Servicios malformados: {row[6]}")

            fechas.append({
                "id":          row[0],
                "fecha":       row[1],
                "lead_id":     row[2],
                "titulo":      row[3],
                "notas":       row[4],
                "ticket":      row[5],
                "servicios":   servicios,
                "lead_nombre": row[7],
                "anio":        row[8]
            })

        # 3) Colores manuales
        cursor.execute("SELECT anio, color FROM anio_color")
        colores = {int(r[0]): r[1] for r in cursor.fetchall()}

        return jsonify({"fechas": fechas, "colores": colores}), 200

    except Exception as e:
        # Loguea el error completo en Heroku
        app.logger.exception("Error en /calendario/fechas_ocupadas")

        # Devuelve un cuerpo “vacío” pero con status 200 para no romper la UI
        return jsonify({"fechas": [], "colores": {}}), 200

    finally:
        if conn:
            liberar_db(conn)


@app.route("/calendario/check", methods=["GET"])
def check_disponibilidad():
    fecha_str = request.args.get("fecha")  # "2025-08-09" (YYYY-MM-DD)
    if not fecha_str:
        return jsonify({"error": "Falta parámetro fecha"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM calendario WHERE fecha = %s", (fecha_str,))
        existe = cursor.fetchone()[0]
        disponible = (existe == 0)  # True si no está en la tabla
        return jsonify({"available": disponible}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


@app.route("/calendario/reservar", methods=["POST"])
def reservar_fecha():
    data = request.json
    fecha_str = data.get("fecha")     # "YYYY-MM-DD"
    lead_id = data.get("lead_id")     # int

    if not fecha_str:
        return jsonify({"error": "No se especificó la fecha"}), 400
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        # Intentar insertar
        cursor.execute("""
            INSERT INTO calendario (fecha, lead_id)
            VALUES (%s, %s)
            ON CONFLICT (fecha) DO NOTHING
        """, (fecha_str, lead_id))
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({
                "ok": False,
                "mensaje": f"La fecha {fecha_str} ya está ocupada"
            }), 200

        return jsonify({
            "ok": True,
            "mensaje": f"Reserva creada para {fecha_str}"
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
@app.route("/calendario/detalle/<int:cal_id>", methods=["GET"])
def detalle_calendario(cal_id):
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, fecha, lead_id, titulo, notas, ticket, servicios
            FROM calendario
            WHERE id = %s
        """, (cal_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Registro no encontrado"}), 404

        # row = (1, datetime.date(2025,8,9), 3, "Boda", "notas...", Decimal('5000.00'), {...} )
        respuesta = {
            "id": row[0],
            "fecha": str(row[1]),  
            "lead_id": row[2],
            "titulo": row[3] or "",
            "notas": row[4] or "",
            "ticket": float(row[5]) if row[5] else 0.0,
            "servicios": row[6] if row[6] else {}
        }
        return jsonify(respuesta), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

@app.route("/calendario/eliminar/<int:cal_id>", methods=["POST"])
def eliminar_calendario(cal_id):
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM calendario WHERE id = %s", (cal_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "No se encontró ese ID"}), 404

        return jsonify({"ok": True, "mensaje": "Fecha eliminada"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

@app.route("/calendario/editar/<int:cal_id>", methods=["POST"])
def editar_calendario(cal_id):
    data = request.json
    titulo = data.get("titulo", "")
    notas = data.get("notas", "")
    ticket = data.get("ticket", 0)
    servicios_input = data.get("servicios", {})

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        # Convertir ticket
        ticket_value = float(ticket) if ticket else 0.0

        import json
        # Convertir servicios a un JSON string
        if not isinstance(servicios_input, dict):
            servicios_input = {}

        cursor.execute("""
            UPDATE calendario
            SET titulo = %s,
                notas = %s,
                ticket = %s,
                servicios = %s::jsonb
            WHERE id = %s
        """, (
            titulo,
            notas,
            ticket_value,
            json.dumps(servicios_input),
            cal_id
        ))
        conn.commit()

        if cursor.rowcount == 0:
            # Significa que no se actualizó nada: puede que no exista ese ID
            return jsonify({"error": "No se encontró esa fecha o no se modificó nada"}), 404

        return jsonify({"ok": True, "mensaje": "Fecha actualizada correctamente"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
#Actualizar cambio de color
@app.route("/calendario/anio_color", methods=["POST"])
def actualizar_color_anio():
    data = request.get_json()
    anio  = data.get("anio")
    color = data.get("color")
    if not anio or not color:
        return jsonify({"error": "Faltan datos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB off"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO anio_color (anio, color)
            VALUES (%s,%s)
            ON CONFLICT (anio) DO UPDATE SET color=EXCLUDED.color
        """, (anio, color))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

#Funcion para eliminar Año con odos sus datos y contraseña
@app.route("/calendario/anio/<int:anio>", methods=["DELETE"])
def eliminar_anio(anio):
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB off"}), 500
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM calendario WHERE EXTRACT(YEAR FROM fecha)=%s", (anio,))
        cur.execute("DELETE FROM anio_color WHERE anio=%s", (anio,))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


     
  
    
#'''''''''''''''''''''''''''''''''''''''''''''''
#------------SECION DE REPORTES-----------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,   
        
@app.route("/reportes/ingresos", methods=["GET"])
def reporte_ingresos():
    mes = request.args.get("mes")
    anio = request.args.get("anio")
    if not mes or not anio:
        return jsonify({"error": "Falta mes o año"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(ticket), 0)
            FROM calendario
            WHERE EXTRACT(MONTH FROM fecha) = %s
              AND EXTRACT(YEAR FROM fecha) = %s
        """, (mes, anio))
        total = cursor.fetchone()[0] or 0
        return jsonify({
            "mes": int(mes),
            "anio": int(anio),
            "total_ventas": float(total)
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
        
@app.route("/reportes/ingresos_anual", methods=["GET"])
def reporte_ingresos_anual():
    anio = request.args.get("anio")
    if not anio:
        return jsonify({"error": "Falta el parámetro año"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # ── 1) Ingresos por mes ─────────────────────────
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM fecha)::int   AS mes,
                   COALESCE(SUM(ticket),0)          AS total_ingresos
            FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
            GROUP BY mes
        """, (anio,))
        ingresos_por_mes = {m:0.0 for m in range(1,13)}
        for mes, total in cursor.fetchall():
            ingresos_por_mes[int(mes)] = float(total)

        # ── 2) Gastos reales por mes ────────────────────
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM fecha)::int   AS mes,
                   COALESCE(SUM(monto),0)           AS total_gastos
            FROM gastos
            WHERE EXTRACT(YEAR FROM fecha) = %s
            GROUP BY mes
        """, (anio,))
        gastos_por_mes = {m:0.0 for m in range(1,13)}
        for mes, total in cursor.fetchall():
            gastos_por_mes[int(mes)] = float(total)

        # ── 3) Costos finales = max(gastos, 30 % ingresos) ──
        costos_por_mes = {}
        for m in range(1,13):
            min_30 = ingresos_por_mes[m] * 0.30
            costos_por_mes[m] = max(gastos_por_mes[m], min_30)

        # ── 4) Número total de eventos del año ──────────
        cursor.execute("""
            SELECT COUNT(*) FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
        """, (anio,))
        total_eventos = cursor.fetchone()[0] or 0

        return jsonify({
            "anio": int(anio),
            "ingresos_anual": ingresos_por_mes,
            "costos_anual":   costos_por_mes,   # ← devuelvo el AJUSTADO
            "total_eventos":  total_eventos
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)



@app.route("/reportes/servicios_anual", methods=["GET"])
def reporte_servicios_anual():
    anio = request.args.get("anio")
    if not anio:
        return jsonify({"error": "Falta año"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COALESCE(SUM((servicios->>'letrasGigantes')::int), 0),
                COALESCE(SUM((servicios->>'chisperos')::int), 0),
                COALESCE(SUM((servicios->>'cabinaFotos')::int), 0),
                COALESCE(SUM((servicios->>'cabina360')::int), 0),
                COALESCE(SUM((servicios->>'caritoDeShotsSinAlcohol')::int), 0),
                COALESCE(SUM((servicios->>'caritoDeShotsConAlcohol')::int), 0),
                COALESCE(SUM((servicios->>'lluviaDeMariposas')::int), 0),
                COALESCE(SUM((servicios->>'lluviaMetalica')::int), 0),
                COALESCE(SUM((servicios->>'nieblaDePiso')::int), 0),
                COALESCE(SUM((servicios->>'scrapbook')::int), 0),
                COALESCE(SUM((servicios->>'audioGuestBook')::int), 0),
                COUNT(*)
            FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
        """, (anio,))
        row = cursor.fetchone()
        
        # row = (letrasGigantes, chisperos, cabinaFotos, cabina360, shotsSin, shotsCon, lluviaM, lluviaMetalica, niebla, scrapbook, audioGB, totalEventos)
        return jsonify({
            "anio": int(anio),
            "letrasGigantes": row[0],
            "chisperos": row[1],
            "cabinaFotos": row[2],
            "cabina360": row[3],
            "caritoDeShotsSinAlcohol": row[4],
            "caritoDeShotsConAlcohol": row[5],
            "lluviaDeMariposas": row[6],
            "lluviaMetalica": row[7],
            "nieblaDePiso": row[8],
            "scrapbook": row[9],
            "audioGuestBook": row[10],
            "eventosContados": row[11]
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)




#'''''''''''''''''''''''''''''''''''''''''''''''
#--------------SECION DE GASTOS-----------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,   
        

@app.route("/gastos/agregar", methods=["POST"])
def agregar_gasto():
    data = request.json
    monto = data.get("monto", 0)
    etiqueta = data.get("etiqueta", "")
    descripcion = data.get("descripcion", "")

    # Validación básica: monto debe ser mayor que 0
    if not monto or float(monto) <= 0:
        return jsonify({"error": "El monto debe ser mayor que 0"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO gastos (monto, etiqueta, descripcion, fecha)
            VALUES (%s, %s, %s, NOW())
        """, (monto, etiqueta, descripcion))
        conn.commit()

        return jsonify({"ok": True, "mensaje": "Gasto registrado correctamente."}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


@app.route("/gastos/agregar_etiqueta", methods=["POST"])
def agregar_etiqueta():
    data = request.json
    etiqueta = data.get("etiqueta")
    if not etiqueta:
        return jsonify({"error": "Falta el nombre de la etiqueta"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO gasto_etiquetas (etiqueta)
            VALUES (%s)
            ON CONFLICT (etiqueta) DO NOTHING
        """, (etiqueta,))
        conn.commit()

        return jsonify({"ok": True, "mensaje": "Etiqueta creada correctamente."}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

 
        
# GET  /gastos/etiquetas  →  [{etiqueta:"Renta", color:"#ff9800"}, …]
@app.route("/gastos/etiquetas", methods=["GET"])
def listar_etiquetas():
    conn = conectar_db();              # ← tu helper
    if not conn:
        return jsonify({"error":"DB off"}), 500
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT etiqueta, COALESCE(color,'') AS color
            FROM   gasto_etiquetas
            ORDER  BY etiqueta
        """)
        return jsonify({"etiquetas": cur.fetchall()}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


# POST /gastos/etiqueta_color  { etiqueta:"Renta", color:"#ff9800" }
@app.route("/gastos/etiqueta_color", methods=["POST"])
def actualizar_color_etiqueta():
    data      = request.get_json()
    etiqueta  = data.get("etiqueta")
    color     = data.get("color")

    if not etiqueta or not color:
        return jsonify({"error":"Faltan datos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error":"DB off"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE gasto_etiquetas
            SET    color = %s
            WHERE  etiqueta = %s
        """, (color, etiqueta))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)



@app.route("/gastos/por_etiqueta", methods=["GET"])
def gastos_por_etiqueta():
    etiqueta = request.args.get("etiqueta")
    if not etiqueta:
        return jsonify({"error": "Falta el parámetro 'etiqueta'"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500
    
    try:
        cursor = conn.cursor()
        # Se obtiene la lista de gastos para la etiqueta solicitada
        cursor.execute("""
            SELECT id, monto, descripcion, fecha
            FROM gastos
            WHERE etiqueta = %s
            ORDER BY fecha DESC
        """, (etiqueta,))
        rows = cursor.fetchall()
        gastos = []
        for row in rows:
            gastos.append({
                "id": row[0],
                "monto": float(row[1]),
                "descripcion": row[2],
                "fecha": row[3].strftime("%Y-%m-%d")  # o el formato que prefieras

            })
        return jsonify({"gastos": gastos}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

# 📌 Endpoint para eliminar un registro individual
@app.route("/gastos/eliminar/<int:gasto_id>", methods=["POST"])
def eliminar_gasto(gasto_id):
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la BD"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM gastos WHERE id = %s", (gasto_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "No existe gasto con ese id"}), 404
        return jsonify({"ok": True, "mensaje": "Gasto eliminado"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

# 📌 Endpoint para eliminar la etiqueta completa
@app.route("/gastos/eliminar_etiqueta", methods=["POST"])
def eliminar_etiqueta():
    data = request.json
    etiqueta = data.get("etiqueta")
    if not etiqueta:
        return jsonify({"error": "No se indicó la etiqueta"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar"}), 500

    try:
        cursor = conn.cursor()
        # primero borrar gastos con esa etiqueta
        cursor.execute("DELETE FROM gastos WHERE etiqueta = %s", (etiqueta,))
        # luego borrar la etiqueta de la tabla gasto_etiquetas
        cursor.execute("DELETE FROM gasto_etiquetas WHERE etiqueta = %s", (etiqueta,))
        conn.commit()
        return jsonify({"ok": True, "mensaje": f"Etiqueta {etiqueta} eliminada"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
        
# RUTA PARA SUBIR LOGO 
@app.route("/config/logo", methods=["POST"])
def subir_logo():
    file = request.files.get("logo")
    if not file or file.filename == "":
        return jsonify({"error": "Archivo inválido"}), 400

    # 1) Convertir a data-URI
    mime = file.content_type  # p.e. 'image/png'
    data = base64.b64encode(file.read()).decode()  
    uri  = f"data:{mime};base64,{data}"

    # 2) Guardar en config
    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB no disponible")
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO config (clave, valor)
            VALUES ('logo_base64', %s)
            ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor
        """, (uri,))
        conn.commit()
    finally:
        liberar_db(conn)

    # 3) Responder al cliente con la URL nueva
    return jsonify({"url": uri}), 200




# RUTA PARA OBTENER LOGO
@app.route("/config/logo", methods=["GET"])
def obtener_logo():
    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB no disponible")
        cur = conn.cursor()
        cur.execute("SELECT valor FROM config WHERE clave='logo_base64'")
        row = cur.fetchone()
    finally:
        liberar_db(conn)

    # Si existe un valor, lo devolvemos; si no, fallback a un default estático
    if row and row[0]:
        return jsonify({"url": row[0]}), 200

    # Fallback
    return jsonify({"url": "/static/logo/default.png"}), 200



# 📌 Endpoint para Mensajería
@app.route("/config/mensajeria", methods=["GET","POST"])
def config_mensajeria():
    if request.method == "GET":
        conn = conectar_db(); cur = conn.cursor()
        cur.execute("SELECT clave,valor FROM config WHERE clave LIKE 'mensajeria:%'")
        rows = cur.fetchall(); liberar_db(conn)
        return jsonify({k.split(":",1)[1]:v for k,v in rows})
    # POST
    data = request.json or {}
    conn = conectar_db(); cur = conn.cursor()
    for k,v in data.items():
        cur.execute("""INSERT INTO config(clave,valor)
                       VALUES (%s,%s)
                       ON CONFLICT(clave) DO UPDATE SET valor=EXCLUDED.valor""",
                    (f"mensajeria:{k}", v))
    conn.commit(); liberar_db(conn)
    return jsonify({"ok":True})

# IA (OpenAI)
@app.route("/config/ia", methods=["GET","POST"])
def config_ia():
    if request.method == "GET":
        conn = conectar_db(); cur = conn.cursor()
        cur.execute("SELECT valor FROM config WHERE clave='openai:api_key'")
        row = cur.fetchone(); liberar_db(conn)
        return jsonify({"openai_api_key": row[0] if row else ""})
    key = request.json.get("openai_api_key","")
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO config(clave,valor)
                   VALUES ('openai:api_key',%s)
                   ON CONFLICT(clave) DO UPDATE SET valor=EXCLUDED.valor""",
                (key,))
    conn.commit(); liberar_db(conn)
    return jsonify({"ok":True})

# n8n
@app.route("/config/n8n", methods=["GET","POST"])
def config_n8n():
    if request.method == "GET":
        conn = conectar_db(); cur = conn.cursor()
        cur.execute("SELECT clave,valor FROM config WHERE clave LIKE 'n8n:%'")
        rows = cur.fetchall(); liberar_db(conn)
        return jsonify({k.split(":",1)[1]:v for k,v in rows})
    data = request.json or {}
    conn = conectar_db(); cur = conn.cursor()
    for k,v in data.items():
        cur.execute("""INSERT INTO config(clave,valor)
                       VALUES (%s,%s)
                       ON CONFLICT(clave) DO UPDATE SET valor=EXCLUDED.valor""",
                    (f"n8n:{k}", v))
    conn.commit(); liberar_db(conn)
    return jsonify({"ok":True})


# Decorador genérico que verifica permisos antes de ejecutar un endpoin
def requires_permission(action):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = g.current_user
            if not user.has_permission(action):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator

# Proteccion de Rutas
@app.route("/pipeline/mover", methods=["POST"])
@requires_permission("move_pipeline")
def mover_pipeline():
    # lógica para mover lead
    ...


 
# 📌 Endpoint para gestión de usuarios y roles en el CRM
# Helper: decorator de permisos (asume que g.current_user está cargado)
def requires_permission(action):
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = g.current_user
            if not user.has_permission(action):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator

# 1) GET /users?tenant_id=...
@app.route("/users", methods=["GET"])
@requires_permission("view_users")
def listar_usuarios():
    tenant_id = request.args.get("tenant_id", type=int)
    if not tenant_id:
        return jsonify({"error":"tenant_id requerido"}), 400

    conn = conectar_db(); cur = conn.cursor()
    # Traemos usuario y sus roles en array
    cur.execute("""
      SELECT u.id, u.email,
        ARRAY(
          SELECT r.name
          FROM user_roles ur
          JOIN roles r ON ur.role_id = r.id
          WHERE ur.user_id = u.id
        ) AS roles
      FROM users u
      WHERE u.tenant_id = %s
    """, (tenant_id,))
    rows = cur.fetchall()
    liberar_db(conn)

    usuarios = [{"id":r[0], "email":r[1], "roles": r[2]} for r in rows]
    return jsonify(usuarios), 200


# 2) POST /users/invite
@app.route("/users/invite", methods=["POST"])
@requires_permission("manage_users")
def invitar_usuario():
    data = request.json or {}
    email     = data.get("email", "").strip()
    tenant_id = data.get("tenant_id")
    if not email or not tenant_id:
        return jsonify({"error":"email y tenant_id son requeridos"}), 400

    # Generar password temporal o token de registro
    temp_password = uuid.uuid4().hex[:8]
    pw_hash = generate_password_hash(temp_password)

    conn = conectar_db(); cur = conn.cursor()
    try:
        # Crear usuario con rol 'seller' por defecto
        cur.execute("""
          INSERT INTO users (email, password_hash, tenant_id)
          VALUES (%s, %s, %s)
          RETURNING id
        """, (email, pw_hash, tenant_id))
        user_id = cur.fetchone()[0]
        # Asignar rol 'seller'
        cur.execute("""
          INSERT INTO user_roles (user_id, role_id)
          SELECT %s, id FROM roles WHERE name='seller'
        """, (user_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        liberar_db(conn)
        return jsonify({"error":str(e)}), 500
    liberar_db(conn)

    # Enviar email con credenciales temporales y/o link de registro
    # aquí pondrías tu lógica de envío de correo...
    enviar_email(
      to=email,
      subject="Invitación a tu CRM",
      body=f"Te hemos invitado. Tu contraseña temporal es: {temp_password}"
    )

    return jsonify({"ok":True, "user_id": user_id}), 201


# 3) POST /users/<id>/roles
@app.route("/users/<int:user_id>/roles", methods=["POST"])
@requires_permission("manage_users")
def actualizar_roles(user_id):
    data = request.json or {}
    roles = data.get("roles")
    if not isinstance(roles, list):
        return jsonify({"error":"Se requiere un array 'roles'"}), 400

    conn = conectar_db(); cur = conn.cursor()
    try:
        # Borrar roles previos
        cur.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
        # Insertar nuevos
        for role_name in roles:
            cur.execute("""
              INSERT INTO user_roles (user_id, role_id)
              SELECT %s, id FROM roles WHERE name = %s
            """, (user_id, role_name))
        conn.commit()
    except Exception as e:
        conn.rollback()
        liberar_db(conn)
        return jsonify({"error":str(e)}), 500
    liberar_db(conn)

    return jsonify({"ok":True}), 200



# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    
    
