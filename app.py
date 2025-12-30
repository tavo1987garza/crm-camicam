
from dotenv import load_dotenv
import os
import json
import re
from werkzeug.security import generate_password_hash, check_password_hash
import time
import base64
import uuid 
from datetime import datetime, timezone, date, timedelta
import requests
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import secrets
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from flask import (
    Flask, request, jsonify, render_template, send_from_directory,
    current_app, redirect, url_for, session, g, abort, flash
)
from flask_socketio import SocketIO

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

from functools import wraps

load_dotenv()


@app.before_request
def cargar_usuario_actual():
    """
    Carga el usuario actual en g.current_user basado en la sesión y el cliente_id del subdominio.
    """
    g.current_user = None
    
    # Obtener cliente_id del subdominio
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return  # No hay cliente, no hay usuario

    # Si hay sesión activa, cargar el usuario
    user_id = session.get('user_id')
    if user_id:
        conn = conectar_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, email, cliente_id 
                    FROM users 
                    WHERE id = %s AND cliente_id = %s AND activo = true
                """, (user_id, cliente_id))
                row = cur.fetchone()
                if row:
                    g.current_user = {
                        'id': row[0],
                        'email': row[1],
                        'cliente_id': row[2]
                    }
            finally:
                liberar_db(conn)

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
# Detectar el subdominio en cada petición. 
# Obtener el cliente_id correspondiente.
# Inyectar ese cliente_id en cada consulta a la base de datos.
##################################   

def obtener_cliente_id_de_subdominio():
    """
    Extrae el subdominio de request.host y devuelve el cliente_id.
    Ej: crm.cami-cam.com → subdominio = 'crm' → cliente_id = 1
    """
    host = request.host.lower()
    if host == "localhost:5000" or host.startswith("127.0.0.1"):
        # En desarrollo, usa el cliente por defecto (id=1)
        return 1

    # Extraer subdominio: "cliente1.cami-cam.com" → "cliente1"
    partes = host.split('.')
    if len(partes) < 3:
        # Dominio sin subdominio (ej: cami-cam.com) → error
        return None

    subdominio = partes[0]
    if subdominio in ("www", "cotizador"):  # Excluir subdominios especiales
        return None

    # Buscar cliente en la base de datos
    conn = conectar_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM clientes WHERE subdominio = %s AND activo = true", (subdominio,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        liberar_db(conn)
        
        
##################################
#----------SECCION PANEL----------
##################################   

# 📌 Endpoint para el buscador de fecha
@app.route("/calendario/checar_fecha")
def checar_fecha():
    fecha = request.args.get("fecha")
    if not fecha:
        return jsonify({"error": "Falta parámetro 'fecha'"}), 400
    
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"count": 0}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"count": 0}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM calendario
            WHERE DATE(fecha AT TIME ZONE 'UTC') = %s AND cliente_id = %s
        """, (fecha, cliente_id))
        cnt = cur.fetchone()[0]
        return jsonify({"count": cnt}), 200
    except Exception as e:
        app.logger.exception("Error en /calendario/checar_fecha")
        return jsonify({"count": 0}), 500
    finally:
        liberar_db(conn)
        
        
# 📌 Endpoint para la visualzacion de Próximos eventos   
@app.route("/calendario/proximos")
def proximos_eventos():
    try:
        cliente_id = obtener_cliente_id_de_subdominio()
        if not cliente_id:
            return jsonify([]), 404

        lim = int(request.args.get("limite", 5))
        conn = conectar_db()
        if not conn:
            return jsonify([]), 500
        try:
            cur = conn.cursor()
            cur.execute("""
              SELECT id,
                     TO_CHAR(fecha AT TIME ZONE 'UTC','YYYY-MM-DD'),
                     COALESCE(titulo,''),
                     COALESCE(servicios::text,'{}')
              FROM calendario
              WHERE cliente_id = %s AND fecha AT TIME ZONE 'UTC' >= %s
              ORDER BY fecha ASC
              LIMIT %s
            """, (cliente_id, date.today(), lim))
            rows = cur.fetchall()
            out = []
            for id_, fecha, titulo, servicios_text in rows:
                try:
                    servicios = json.loads(servicios_text)
                except:
                    servicios = {}
                out.append({"id": id_, "fecha": fecha, "titulo": titulo, "servicios": servicios})
            return jsonify(out), 200
        finally:
            liberar_db(conn)
    except Exception:
        current_app.logger.exception("Error en /calendario/proximos")
        return jsonify([]), 200

# 📌 Endpoint para mostras los Ultimos Leads    
@app.route("/leads/ultimos")
def ultimos_leads():
    try:
        cliente_id = obtener_cliente_id_de_subdominio()
        if not cliente_id:
            return jsonify([]), 404

        lim = int(request.args.get("limite", 3))
        conn = conectar_db()
        if not conn:
            return jsonify([]), 500
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(
                "SELECT id, nombre, telefono FROM leads WHERE cliente_id = %s ORDER BY id DESC LIMIT %s",
                (cliente_id, lim)
            )
            rows = cur.fetchall()
            return jsonify(rows), 200
        finally:
            liberar_db(conn)
    except Exception:
        current_app.logger.exception("Error en /leads/ultimos")
        return jsonify([]), 200

# 📌 Endpoint para mostrar el KPI ensual (La meta mensual)
@app.route("/reportes/kpi_mes")
def kpi_mes():
    try:
        cliente_id = obtener_cliente_id_de_subdominio()
        if not cliente_id:
            return jsonify({"actual": 0, "meta": 15}), 404

        hoy = datetime.utcnow()
        mes, anio = hoy.month, hoy.year
        conn = conectar_db()
        if not conn:
            return jsonify({"actual": 0, "meta": 15}), 500
        try:
            cur = conn.cursor()
            cur.execute("""
              SELECT COUNT(*) FROM calendario
               WHERE EXTRACT(YEAR FROM fecha AT TIME ZONE 'UTC')=%s
                 AND EXTRACT(MONTH FROM fecha AT TIME ZONE 'UTC')=%s
                 AND cliente_id = %s
            """, (anio, mes, cliente_id))
            actual = cur.fetchone()[0]
            cur.execute("SELECT valor FROM config WHERE clave='meta_mensual' AND cliente_id = %s", (cliente_id,))
            row = cur.fetchone()
            meta = int(row[0]) if row and row[0].isdigit() else 15
            return jsonify({"actual": actual, "meta": meta}), 200
        finally:
            liberar_db(conn)
    except Exception:
        current_app.logger.exception("Error en /reportes/kpi_mes")
        return jsonify({"actual": 0, "meta": 15}), 200

##################################
#----------SECCION LEADS---------- 
##################################   

# 📌 NUEVO: Guardar contexto del bot
@app.route("/leads/context", methods=["POST"])
def guardar_contexto_lead():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    datos = request.json
    telefono = datos.get("telefono")
    contexto = datos.get("contexto")
    
    if not telefono or not contexto:
        return jsonify({"error": "Faltan datos: telefono o context"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a BD"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leads (telefono, nombre, estado, contexto, last_activity, cliente_id) 
            VALUES (%s, %s, 'Contacto Inicial', %s, NOW(), %s)
            ON CONFLICT (telefono, cliente_id) 
            DO UPDATE SET 
                contexto = EXCLUDED.contexto, 
                last_activity = NOW(),
                estado = CASE 
                    WHEN leads.estado = 'Finalizado' THEN 'Contacto Inicial' 
                    ELSE leads.estado 
                END
            RETURNING id
        """, (telefono, f"Lead {telefono[-4:]}", json.dumps(contexto), cliente_id))
        
        conn.commit()
        return jsonify({"mensaje": "Contexto guardado"}), 200
    except Exception as e:
        print(f"❌ Error en /leads/context: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        

        
        
@app.route("/leads/context", methods=["GET"])   
def obtener_contexto_lead():
    telefono = request.args.get("telefono")
    if not telefono:
        return jsonify({"error": "Falta teléfono"}), 400
    
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"context": None}), 200

    conn = conectar_db()
    if not conn:
        return jsonify({"context": None}), 200
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT contexto FROM leads WHERE telefono = %s AND cliente_id = %s",
            (telefono, cliente_id)
        )
        row = cursor.fetchone()
        if not row or not row['contexto']:
            return jsonify({"context": None}), 200
        
        contexto_raw = row['contexto']
        if isinstance(contexto_raw, dict):
            contexto = contexto_raw
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
    
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a BD"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM leads WHERE telefono = %s AND cliente_id = %s",
            (telefono, cliente_id)
        )
        row = cursor.fetchone()
        if row:
            return jsonify({"id": row[0]}), 200
        else:
            return jsonify({"error": "Lead no encontrado"}), 404
    except Exception as e:
        app.logger.exception("Error en /lead_id")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)

# 📌 Endpoint para recibir mensajes desde WhatsApp
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    datos = request.json
    plataforma = datos.get("plataforma")
    remitente = str(datos.get("remitente", ""))
    mensaje = datos.get("mensaje")
    tipo = datos.get("tipo")

    if not plataforma or not remitente or mensaje is None:
        return jsonify({"error": "Faltan datos"}), 400

    tipos_validos = {"enviado", "recibido", "recibido_imagen", "enviado_imagen", "recibido_video", "enviado_video"}
    if tipo not in tipos_validos:
        tipo = "recibido"

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # Verificar si el lead ya existe PARA ESTE CLIENTE
        cursor.execute("SELECT id, nombre FROM leads WHERE telefono = %s AND cliente_id = %s", (remitente, cliente_id))
        lead = cursor.fetchone()
        lead_id = None
        lead_creado = False

        if not lead:
            nombre_por_defecto = f"Lead desde Chat {remitente[-10:]}"
            cursor.execute("""
                INSERT INTO leads (nombre, telefono, estado, cliente_id)
                VALUES (%s, %s, 'Contacto Inicial', %s)
                ON CONFLICT (telefono, cliente_id) DO NOTHING
                RETURNING id
            """, (nombre_por_defecto, remitente, cliente_id))
            row = cursor.fetchone()
            if row:
                lead_id = row[0]
                lead_creado = True
        else:
            lead_id = lead[0]

        # Guardar el mensaje con cliente_id
        cursor.execute("""
            INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo, cliente_id)
            VALUES (%s, %s, %s, 'Nuevo', %s, %s)
        """, (plataforma, remitente, mensaje, tipo, cliente_id))
        conn.commit()

        # ... resto del código para eventos y emisión WebSocket ...

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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify([])

    conn = conectar_db()
    if not conn:
        return jsonify([])

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT l.*, 
                   (SELECT mensaje FROM mensajes WHERE remitente = l.telefono AND cliente_id = %s ORDER BY fecha DESC LIMIT 1) as ultimo_mensaje
            FROM leads l
            WHERE l.cliente_id = %s
            ORDER BY l.estado
        """, (cliente_id, cliente_id))
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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    datos = request.json
    nombre = datos.get("nombre")
    telefono = datos.get("telefono")
    notas = datos.get("notas", "")

    if not nombre or not telefono or not validar_telefono(telefono):
        return jsonify({"error": "El teléfono debe tener 13 dígitos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos."}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leads (nombre, telefono, estado, notas, cliente_id)
            VALUES (%s, %s, 'Contacto Inicial', %s, %s)
            ON CONFLICT (telefono, cliente_id) DO UPDATE
            SET notas = EXCLUDED.notas
            RETURNING id
        """, (nombre, telefono, notas, cliente_id))

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
        telefono = datos.get("telefono")
        if not lead_id or not telefono:
            return jsonify({"error": "Faltan datos"}), 400

        cliente_id = obtener_cliente_id_de_subdominio()
        if not cliente_id:
            return jsonify({"error": "Cliente no autorizado"}), 404

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "No se pudo conectar a la base de datos"}), 500
        cursor = conn.cursor()
        # 🔹 Eliminar solo si pertenece al cliente actual
        cursor.execute("DELETE FROM mensajes WHERE remitente = %s AND cliente_id = %s", (telefono, cliente_id))
        cursor.execute("DELETE FROM leads WHERE id = %s AND cliente_id = %s", (lead_id, cliente_id))
        conn.commit()
        conn.close()

        # Notificar al bot
        try:
            requests.post(
                f"{CAMIBOT_API_URL}/limpiar_contexto",
                json={"telefono": telefono},
                timeout=5
            )
        except Exception as e:
            app.logger.warning(f"⚠️ No se pudo notificar al bot al eliminar lead {telefono}: {e}")

        socketio.emit("lead_eliminado", {"id": lead_id, "telefono": telefono})
        return jsonify({"mensaje": "Lead y sus mensajes eliminados correctamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

@app.route('/editar_lead', methods=['POST'])
def editar_lead():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    data = request.get_json()
    lead_id = data.get("id")
    nuevo_nombre = data.get("nombre").strip() if data.get("nombre") else None
    nuevo_telefono = data.get("telefono").strip() if data.get("telefono") else None
    nuevas_notas = data.get("notas").strip() if data.get("notas") else ""

    if not lead_id or not nuevo_telefono:
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
            WHERE id = %s AND cliente_id = %s
        """, (nuevo_nombre, nuevo_telefono, nuevas_notas, lead_id, cliente_id))
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({"error": "Lead no encontrado"}), 404

        return jsonify({"mensaje": "Lead actualizado correctamente"}), 200
    except Exception as e:
        print(f"❌ Error en /editar_lead: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
# 📌 Obtener mensajes
@app.route("/mensajes", methods=["GET"])
def obtener_mensajes():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify([])

    conn = conectar_db()
    if not conn:
        return jsonify([])

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM mensajes WHERE cliente_id = %s ORDER BY fecha DESC", (cliente_id,))
        mensajes = cursor.fetchall()
        return jsonify(mensajes)
    except Exception as e:
        print("❌ Error en /mensajes:", str(e))
        return jsonify([])
    finally:
        liberar_db(conn)
        
        
# 📌 Actualizar estado de mensaje 
@app.route("/actualizar_estado", methods=["POST"])
def actualizar_estado():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    datos = request.json
    mensaje_id = datos.get("id")
    nuevo_estado = datos.get("estado")

    if not mensaje_id or nuevo_estado not in ["Nuevo", "En proceso", "Finalizado"]:
        return jsonify({"error": "Datos incorrectos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE mensajes SET estado = %s WHERE id = %s AND cliente_id = %s",
            (nuevo_estado, mensaje_id, cliente_id)
        )
        conn.commit()
        
        if cursor.rowcount == 0:
            return jsonify({"error": "Mensaje no encontrado"}), 404
            
        return jsonify({"mensaje": "Estado actualizado correctamente"}), 200
    except Exception as e:
        print(f"❌ Error en /actualizar_estado: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
#obtener los mensajes de un remitente específico Devuelve los mensajes en el formato esperado por el frontend.
# Mostrar los mensajes de cada chat
@app.route("/mensajes_chat", methods=["GET"])
def obtener_mensajes_chat():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    remitente = request.args.get("id")
    if not remitente:
        return jsonify({"error": "Falta el ID del remitente"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT nombre FROM leads WHERE telefono = %s AND cliente_id = %s", (remitente, cliente_id))
        lead = cursor.fetchone()
        nombre_lead = lead["nombre"] if lead else remitente

        cursor.execute("""
            SELECT * FROM mensajes 
            WHERE remitente = %s AND cliente_id = %s 
            ORDER BY fecha ASC
        """, (remitente, cliente_id))
        mensajes = cursor.fetchall()

        return jsonify({"nombre": nombre_lead, "mensajes": mensajes})
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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"anios": []}), 200

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT EXTRACT(YEAR FROM fecha) as anio,
                   COUNT(*) OVER (PARTITION BY EXTRACT(YEAR FROM fecha)) as total
            FROM calendario
            WHERE cliente_id = %s
            ORDER BY anio DESC
        """, (cliente_id,))
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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"anios": [], "eventos": []}), 200

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No hay DB"}), 500

    try:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT EXTRACT(YEAR FROM fecha) as anio
            FROM calendario
            WHERE cliente_id = %s
            ORDER BY anio DESC
        """, (cliente_id,))
        anios = [int(row[0]) for row in cursor.fetchall()]
        
        cursor.execute("""
            SELECT id, fecha, titulo, notas, ticket, servicios
            FROM calendario
            WHERE cliente_id = %s
            ORDER BY fecha ASC
        """, (cliente_id,))
        
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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    data = request.json
    fecha_str = data.get("fecha")
    force = data.get("force", False)
    
    try:
        fecha_utc = datetime.strptime(fecha_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        fecha_local = fecha_utc.astimezone()
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Use YYYY-MM-DD"}), 400
    
    lead_id = data.get("lead_id")
    titulo = data.get("titulo", "")
    notas = data.get("notas", "")
    ticket = data.get("ticket", 0)
    servicios_input = data.get("servicios")

    if not fecha_str:
        return jsonify({"error": "Falta la fecha en formato YYYY-MM-DD"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        ticket_value = float(ticket) if ticket else 0.0

        if isinstance(servicios_input, str):
            try:
                servicios_json = json.loads(servicios_input)
            except:
                servicios_json = {}
        elif isinstance(servicios_input, dict):
            servicios_json = servicios_input
        else:
            servicios_json = {}

        # 🔹 Contar eventos del MISMO cliente en esa fecha
        cursor.execute(
            "SELECT COUNT(*) FROM calendario WHERE fecha = %s AND cliente_id = %s",
            (fecha_str, cliente_id)
        )
        ya_hay = cursor.fetchone()[0]

        if ya_hay >= 4:
            return jsonify({
                "ok": False,
                "mensaje": f"El {fecha_str} ha alcanzado el límite de 4 eventos."
            }), 200
        if ya_hay in (1, 2, 3) and not force:
            return jsonify({
                "ok": False,
                "second_possible": True,
                "mensaje": f"Ya hay {ya_hay} evento(s) el {fecha_str}. ¿Agregar otro?"
            }), 200

        # 🔹 Insertar con cliente_id
        cursor.execute("""
            INSERT INTO calendario (fecha, lead_id, titulo, notas, ticket, servicios, cliente_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
        """, (
            fecha_local.date(),
            lead_id,
            titulo,
            notas,
            ticket_value,
            json.dumps(servicios_json),
            cliente_id
        ))
        conn.commit()

        socketio.emit(
            "calendario_actualizado",
            {"accion": "nueva_fecha", "anio": fecha_local.year, "fecha": fecha_str, "titulo": titulo}
        )

        return jsonify({
            "ok": True,
            "mensaje": f"Fecha {fecha_str} agregada correctamente."
        }), 200

    except Exception as e:
        print(f"❌ Error en /calendario/agregar_manual: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
  
# 📌 Obtener todas las fechas ocupadas + colores por año
@app.route("/calendario/fechas_ocupadas", methods=["GET"])
def fechas_ocupadas():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"fechas": [], "colores": {}}), 200

    conn = None
    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("No hay conexión a la base de datos")

        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                c.id, TO_CHAR(c.fecha AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS fecha,
                c.lead_id, COALESCE(c.titulo, ''), COALESCE(c.notas, ''),
                COALESCE(c.ticket, 0)::float, c.servicios::text, l.nombre,
                EXTRACT(YEAR FROM c.fecha AT TIME ZONE 'UTC')::int AS anio
            FROM calendario c
            LEFT JOIN leads l ON c.lead_id = l.id AND l.cliente_id = %s
            WHERE c.cliente_id = %s
            ORDER BY c.fecha DESC
        """, (cliente_id, cliente_id))
        filas = cursor.fetchall()

        fechas = []
        for row in filas:
            try:
                servicios = json.loads(row[6])
            except Exception:
                servicios = {}
            fechas.append({
                "id": row[0], "fecha": row[1], "lead_id": row[2],
                "titulo": row[3], "notas": row[4], "ticket": row[5],
                "servicios": servicios, "lead_nombre": row[7], "anio": row[8]
            })

        cursor.execute("SELECT anio, color FROM anio_color WHERE cliente_id = %s", (cliente_id,))
        colores = {int(r[0]): r[1] for r in cursor.fetchall()}

        return jsonify({"fechas": fechas, "colores": colores}), 200

    except Exception as e:
        app.logger.exception("Error en /calendario/fechas_ocupadas")
        return jsonify({"fechas": [], "colores": {}}), 200
    finally:
        if conn:
            liberar_db(conn)
            

@app.route("/calendario/check", methods=["GET"])
def check_disponibilidad():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"available": True}), 200  # o False, según prefieras

    fecha_str = request.args.get("fecha")
    if not fecha_str:
        return jsonify({"error": "Falta parámetro fecha"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM calendario WHERE fecha = %s AND cliente_id = %s",
            (fecha_str, cliente_id)
        )
        existe = cursor.fetchone()[0]
        disponible = (existe == 0)
        return jsonify({"available": disponible}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

@app.route("/calendario/reservar", methods=["POST"])
def reservar_fecha():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    data = request.json
    fecha_str = data.get("fecha")
    lead_id = data.get("lead_id")

    if not fecha_str:
        return jsonify({"error": "No se especificó la fecha"}), 400
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO calendario (fecha, lead_id, cliente_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (fecha, cliente_id) DO NOTHING
        """, (fecha_str, lead_id, cliente_id))
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
        
        
        
# Detalle
@app.route("/calendario/detalle/<int:cal_id>", methods=["GET"])
def detalle_calendario(cal_id):
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, fecha, lead_id, titulo, notas, ticket, servicios
            FROM calendario
            WHERE id = %s AND cliente_id = %s
        """, (cal_id, cliente_id))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Registro no encontrado"}), 404

        respuesta = {
            "id": row[0], "fecha": str(row[1]), "lead_id": row[2],
            "titulo": row[3] or "", "notas": row[4] or "",
            "ticket": float(row[5]) if row[5] else 0.0,
            "servicios": row[6] if row[6] else {}
        }
        return jsonify(respuesta), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

# Eliminar
@app.route("/calendario/eliminar/<int:cal_id>", methods=["POST"])
def eliminar_calendario(cal_id):
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM calendario WHERE id = %s AND cliente_id = %s", (cal_id, cliente_id))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "No se encontró ese ID"}), 404
        return jsonify({"ok": True, "mensaje": "Fecha eliminada"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

# Editar
@app.route("/calendario/editar/<int:cal_id>", methods=["POST"])
def editar_calendario(cal_id):
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

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
        ticket_value = float(ticket) if ticket else 0.0
        if not isinstance(servicios_input, dict):
            servicios_input = {}

        cursor.execute("""
            UPDATE calendario
            SET titulo = %s, notas = %s, ticket = %s, servicios = %s::jsonb
            WHERE id = %s AND cliente_id = %s
        """, (titulo, notas, ticket_value, json.dumps(servicios_input), cal_id, cliente_id))
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({"error": "No se encontró esa fecha"}), 404
        return jsonify({"ok": True, "mensaje": "Fecha actualizada"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
        
#Actualizar cambio de color
@app.route("/calendario/anio_color", methods=["POST"])
def actualizar_color_anio():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    data = request.get_json()
    anio = data.get("anio")
    color = data.get("color")
    if not anio or not color:
        return jsonify({"error": "Faltan datos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB off"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO anio_color (anio, color, cliente_id)
            VALUES (%s, % s, %s)
            ON CONFLICT (anio, cliente_id) DO UPDATE SET color=EXCLUDED.color
        """, (anio, color, cliente_id))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

@app.route("/calendario/anio/<int:anio>", methods=["DELETE"])
def eliminar_anio(anio):
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB off"}), 500
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM calendario WHERE EXTRACT(YEAR FROM fecha)=%s AND cliente_id=%s", (anio, cliente_id))
        cur.execute("DELETE FROM anio_color WHERE anio=%s AND cliente_id=%s", (anio, cliente_id))
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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    data = request.json
    monto = data.get("monto", 0)
    etiqueta = data.get("etiqueta", "")
    descripcion = data.get("descripcion", "")

    if not monto or float(monto) <= 0:
        return jsonify({"error": "El monto debe ser mayor que 0"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO gastos (monto, etiqueta, descripcion, fecha, cliente_id)
            VALUES (%s, %s, %s, NOW(), %s)
        """, (monto, etiqueta, descripcion, cliente_id))
        conn.commit()
        return jsonify({"ok": True, "mensaje": "Gasto registrado correctamente."}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

@app.route("/gastos/agregar_etiqueta", methods=["POST"])
def agregar_etiqueta():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

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
            INSERT INTO gasto_etiquetas (etiqueta, cliente_id)
            VALUES (%s, %s)
            ON CONFLICT (etiqueta, cliente_id) DO NOTHING
        """, (etiqueta, cliente_id))
        conn.commit()
        return jsonify({"ok": True, "mensaje": "Etiqueta creada correctamente."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
 
        
# GET  /gastos/etiquetas  →  [{etiqueta:"Renta", color:"#ff9800"}, …]
@app.route("/gastos/etiquetas", methods=["GET"])
def listar_etiquetas():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"etiquetas": []}), 200

    conn = conectar_db()
    if not conn:
        return jsonify({"error":"DB off"}), 500
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT etiqueta, COALESCE(color,'') AS color
            FROM gasto_etiquetas
            WHERE cliente_id = %s
            ORDER BY etiqueta
        """, (cliente_id,))
        return jsonify({"etiquetas": cur.fetchall()}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

# POST /gastos/etiqueta_color  { etiqueta:"Renta", color:"#ff9800" }
@app.route("/gastos/etiqueta_color", methods=["POST"])
def actualizar_color_etiqueta():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error":"No autorizado"}), 404

    data = request.get_json()
    etiqueta = data.get("etiqueta")
    color = data.get("color")

    if not etiqueta or not color:
        return jsonify({"error":"Faltan datos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error":"DB off"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE gasto_etiquetas
            SET color = %s
            WHERE etiqueta = %s AND cliente_id = %s
        """, (color, etiqueta, cliente_id))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

@app.route("/gastos/por_etiqueta", methods=["GET"])
def gastos_por_etiqueta():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    etiqueta = request.args.get("etiqueta")
    if not etiqueta:
        return jsonify({"error": "Falta el parámetro 'etiqueta'"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500
    
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, monto, descripcion, fecha
            FROM gastos
            WHERE etiqueta = %s AND cliente_id = %s
            ORDER BY fecha DESC
        """, (etiqueta, cliente_id))
        rows = cursor.fetchall()
        gastos = []
        for row in rows:
            gastos.append({
                "id": row[0],
                "monto": float(row[1]),
                "descripcion": row[2],
                "fecha": row[3].strftime("%Y-%m-%d")
            })
        return jsonify({"gastos": gastos}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
# 📌 Endpoint para eliminar un registro individual
@app.route("/gastos/eliminar/<int:gasto_id>", methods=["POST"])
def eliminar_gasto(gasto_id):
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la BD"}), 500
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM gastos WHERE id = %s AND cliente_id = %s", (gasto_id, cliente_id))
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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    data = request.json
    etiqueta = data.get("etiqueta")
    if not etiqueta:
        return jsonify({"error": "No se indicó la etiqueta"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM gastos WHERE etiqueta = %s AND cliente_id = %s", (etiqueta, cliente_id))
        cursor.execute("DELETE FROM gasto_etiquetas WHERE etiqueta = %s AND cliente_id = %s", (etiqueta, cliente_id))
        conn.commit()
        return jsonify({"ok": True, "mensaje": f"Etiqueta {etiqueta} eliminada"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

#'''''''''''''''''''''''''''''''''''''''''''''''
#--------------SECION DE CONFIGURACION-----------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,   
        
# RUTA PARA SUBIR LOGO 
@app.route("/config/logo", methods=["POST"])
def subir_logo():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    file = request.files.get("logo")
    if not file or file.filename == "":
        return jsonify({"error": "Archivo inválido"}), 400

    mime = file.content_type
    data = base64.b64encode(file.read()).decode()  
    uri = f"data:{mime};base64,{data}"

    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB no disponible")
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO config (clave, valor, cliente_id)
            VALUES ('logo_base64', %s, %s)
            ON CONFLICT (clave, cliente_id) DO UPDATE SET valor = EXCLUDED.valor
        """, (uri, cliente_id))
        conn.commit()
    finally:
        liberar_db(conn)

    return jsonify({"url": uri}), 200



# RUTA PARA OBTENER LOGO
@app.route("/config/logo", methods=["GET"])
def obtener_logo():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        # Si no hay cliente, mostrar logo por defecto
        return jsonify({"url": "/static/logo/default.png"}), 200

    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB no disponible")
        cur = conn.cursor()
        cur.execute("SELECT valor FROM config WHERE clave='logo_base64' AND cliente_id = %s", (cliente_id,))
        row = cur.fetchone()
    finally:
        liberar_db(conn)

    if row and row[0]:
        return jsonify({"url": row[0]}), 200

    return jsonify({"url": "/static/logo/default.png"}), 200


# 📌 Endpoint para Mensajería
@app.route("/config/mensajeria", methods=["GET","POST"])
def config_mensajeria():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    if request.method == "GET":
        conn = conectar_db()
        cur = conn.cursor()
        cur.execute("SELECT clave,valor FROM config WHERE clave LIKE 'mensajeria:%' AND cliente_id = %s", (cliente_id,))
        rows = cur.fetchall()
        liberar_db(conn)
        return jsonify({k.split(":",1)[1]:v for k,v in rows})

    data = request.json or {}
    conn = conectar_db()
    cur = conn.cursor()
    for k,v in data.items():
        cur.execute("""
            INSERT INTO config(clave,valor,cliente_id)
            VALUES (%s,%s,%s)
            ON CONFLICT(clave,cliente_id) DO UPDATE SET valor=EXCLUDED.valor
        """, (f"mensajeria:{k}", v, cliente_id))
    conn.commit()
    liberar_db(conn)
    return jsonify({"ok":True})


# IA (OpenAI)
@app.route("/config/ia", methods=["GET","POST"])
def config_ia():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    if request.method == "GET":
        conn = conectar_db()
        cur = conn.cursor()
        cur.execute("SELECT valor FROM config WHERE clave='openai:api_key' AND cliente_id = %s", (cliente_id,))
        row = cur.fetchone()
        liberar_db(conn)
        return jsonify({"openai_api_key": row[0] if row else ""})
    
    key = request.json.get("openai_api_key","")
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO config(clave,valor,cliente_id)
        VALUES ('openai:api_key',%s,%s)
        ON CONFLICT(clave,cliente_id) DO UPDATE SET valor=EXCLUDED.valor
    """, (key, cliente_id))
    conn.commit()
    liberar_db(conn)
    return jsonify({"ok":True})


# n8n
@app.route("/config/n8n", methods=["GET","POST"])
def config_n8n():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    if request.method == "GET":
        conn = conectar_db()
        cur = conn.cursor()
        cur.execute("SELECT clave,valor FROM config WHERE clave LIKE 'n8n:%' AND cliente_id = %s", (cliente_id,))
        rows = cur.fetchall()
        liberar_db(conn)
        return jsonify({k.split(":",1)[1]:v for k,v in rows})
    
    data = request.json or {}
    conn = conectar_db()
    cur = conn.cursor()
    for k,v in data.items():
        cur.execute("""
            INSERT INTO config(clave,valor,cliente_id)
            VALUES (%s,%s,%s)
            ON CONFLICT(clave,cliente_id) DO UPDATE SET valor=EXCLUDED.valor
        """, (f"n8n:{k}", v, cliente_id))
    conn.commit()
    liberar_db(conn)
    return jsonify({"ok":True})

 
#''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''
#--------------CONFIGURACION PARA HACER DE LA APP UN MULTITENANT-----------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,  

# 📌 Endpoint para gestión de usuarios y roles en el CRM
# Decorador genérico que verifica permisos antes de ejecutar un endpoin
def requires_permission(action):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not g.current_user:
                abort(403)
            # Por ahora, asumimos que si hay usuario, tiene permiso
            # Puedes implementar permisos reales más tarde
            return f(*args, **kwargs)
        return wrapped
    return decorator

# Proteccion de Rutas
@app.route("/pipeline/mover", methods=["POST"])
@requires_permission("move_pipeline")
def mover_pipeline():
    # lógica para mover lead
    ...





# Validación de subdominio
def validar_subdominio(subdominio):
    """
    Valida que el subdominio sea seguro:
    - Solo letras, números y guiones
    - Entre 3 y 30 caracteres
    - No empieza/termina con guión
    """
    if not re.match(r'^[a-z0-9]([a-z0-9-]{1,28}[a-z0-9])?$', subdominio.lower()):
        return False
    # Palabras reservadas (no permitidas)
    reservadas = {'www', 'crm', 'cotizador', 'api', 'admin', 'login', 'registro'}
    return subdominio.lower() not in reservadas

@app.route("/check_subdominio")
def check_subdominio():
    subdominio = request.args.get("subdominio", "").strip().lower()
    if not subdominio:
        return jsonify({"disponible": False})
    
    # Validar formato
    if not re.match(r'^[a-z0-9]([a-z0-9-]{1,28}[a-z0-9])?$', subdominio):
        return jsonify({"disponible": False})
    
    # Palabras reservadas
    reservadas = {'www', 'crm', 'cotizador', 'api', 'admin', 'login', 'registro'}
    if subdominio in reservadas:
        return jsonify({"disponible": False})
    
    conn = conectar_db()
    if not conn:
        return jsonify({"disponible": False})
    
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM clientes WHERE subdominio = %s", (subdominio,))
        existe = cur.fetchone() is not None
        return jsonify({"disponible": not existe})
    finally:
        liberar_db(conn)
        
@app.route("/registro")
def pagina_registro():
    return render_template("registro.html")

@app.route("/registro", methods=["POST"])
def registrar_nuevo_cliente():
    """
    Registro público para nuevos clientes SaaS.
    Ejemplo de payload:
    {
        "nombre": "Salón de Fiestas XYZ",
        "subdominio": "salonxyz",
        "email": "dueño@salonxyz.com",
        "plan": "premium"
    }
    """
    try:
        datos = request.json
        nombre = datos.get("nombre", "").strip()
        subdominio = datos.get("subdominio", "").strip().lower()
        email = datos.get("email", "").strip().lower()
        plan = datos.get("plan", "basico").lower()

        # Validaciones
        if not nombre or len(nombre) < 3:
            return jsonify({"error": "Nombre del negocio es requerido (mín. 3 caracteres)"}), 400
        if not subdominio or not validar_subdominio(subdominio):
            return jsonify({"error": "Subdominio inválido. Usa solo letras, números y guiones (3-30 caracteres)."}), 400
        if not email or "@" not in email:
            return jsonify({"error": "Email válido es requerido"}), 400
        if plan not in ["basico", "premium"]:
            return jsonify({"error": "Plan debe ser 'basico' o 'premium'"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión a la base de datos"}), 500

        try:
            cur = conn.cursor()

            # Verificar si el subdominio ya existe
            cur.execute("SELECT id FROM clientes WHERE subdominio = %s", (subdominio,))
            if cur.fetchone():
                return jsonify({"error": "El subdominio ya está en uso. Elige otro."}), 400

            # Crear cliente
            cur.execute("""
                INSERT INTO clientes (subdominio, nombre, email_admin, plan)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (subdominio, nombre, email, plan))
            cliente_id = cur.fetchone()[0]

            # Crear usuario administrador
            temp_password = secrets.token_urlsafe(8)  # Contraseña temporal segura
            pw_hash = generate_password_hash(temp_password)
            cur.execute("""
                INSERT INTO users (email, password_hash, cliente_id, activo)
                VALUES (%s, %s, %s, true)
                RETURNING id
            """, (email, pw_hash, cliente_id))

            # Asignar rol 'admin' (asegúrate de que exista en tu tabla 'roles')
            user_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT %s, id FROM roles WHERE name = 'admin'
            """, (user_id,))

            conn.commit()

            # URL de login personalizada
            login_url = f"https://{subdominio}.cami-cam.com/login"

            # Aquí podrías enviar un email con las credenciales
            # enviar_email_registro(email, subdominio, temp_password)

            return jsonify({
                "mensaje": "Cliente registrado exitosamente",
                "login_url": login_url,
                "subdominio": subdominio
            }), 201

        except Exception as e:
            conn.rollback()
            print(f"❌ Error al registrar cliente: {str(e)}")
            return jsonify({"error": "Error interno al registrar cliente"}), 500
        finally:
            liberar_db(conn)

    except Exception as e:
        print(f"❌ Error en /registro: {str(e)}")
        return jsonify({"error": "Error en la solicitud"}), 400
    
    


@app.route("/login")
def pagina_login():
    """Página de login para cualquier subdominio"""
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def procesar_login():
    """
    Procesa el login y valida que el usuario pertenezca al cliente actual.
    """
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no encontrado"}), 404

    datos = request.json
    email = datos.get("email", "").strip().lower()
    password = datos.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email y contraseña son requeridos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, password_hash 
            FROM users 
            WHERE email = %s AND cliente_id = %s AND activo = true
        """, (email, cliente_id))
        
        user = cur.fetchone()
        
        if not user or not check_password_hash(user[1], password):
            return jsonify({"error": "Credenciales inválidas"}), 401

        # Iniciar sesión
        session['user_id'] = user[0]
        session['cliente_id'] = cliente_id
        
        return jsonify({"mensaje": "Login exitoso"}), 200
        
    except Exception as e:
        print(f"❌ Error en login: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        
        
@app.route("/api/cliente_actual")
def api_cliente_actual():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no encontrado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cur = conn.cursor()
        cur.execute("SELECT nombre, subdominio, plan FROM clientes WHERE id = %s", (cliente_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Cliente no encontrado"}), 404
            
        return jsonify({"nombre": row[0], "subdominio": row[1], "plan": row[2]}), 200
        
    except Exception as e:
        print(f"❌ Error en /api/cliente_actual: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        
        


# Endpoint para actualizar información del cliente (PUT)
@app.route("/api/cliente_actual", methods=["PUT"])
def actualizar_cliente_actual():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no encontrado"}), 404

    datos = request.json
    nombre = datos.get("nombre", "").strip()
    
    if not nombre:
        return jsonify({"error": "Nombre es requerido"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cur = conn.cursor()
        cur.execute("UPDATE clientes SET nombre = %s WHERE id = %s", (nombre, cliente_id))
        conn.commit()
        return jsonify({"mensaje": "Información actualizada"}), 200
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error al actualizar cliente: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)

# Endpoint para cambiar contraseña
@app.route("/api/cambiar_password", methods=["POST"])
def cambiar_password():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no encontrado"}), 404

    if 'user_id' not in session:
        return jsonify({"error": "No autenticado"}), 401

    user_id = session['user_id']
    datos = request.json
    password_actual = datos.get("password_actual")
    password_nueva = datos.get("password_nueva")

    if not password_actual or not password_nueva:
        return jsonify({"error": "Contraseña actual y nueva son requeridas"}), 400

    if len(password_nueva) < 6:
        return jsonify({"error": "La nueva contraseña debe tener al menos 6 caracteres"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cur = conn.cursor()
        # Verificar contraseña actual
        cur.execute("SELECT password_hash FROM users WHERE id = %s AND cliente_id = %s", (user_id, cliente_id))
        row = cur.fetchone()
        
        if not row or not check_password_hash(row[0], password_actual):
            return jsonify({"error": "Contraseña actual incorrecta"}), 401

        # Actualizar contraseña
        nuevo_hash = generate_password_hash(password_nueva)
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (nuevo_hash, user_id))
        conn.commit()
        
        return jsonify({"mensaje": "Contraseña actualizada"}), 200
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error al cambiar contraseña: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        
        
        
def enviar_email_recuperacion(email_destino, reset_url):
    """
    Envía un email de recuperación de contraseña usando SendGrid.
    """
    try:
        message = Mail(
            from_email=os.getenv('SENDGRID_FROM_EMAIL'),
            to_emails=email_destino,
            subject='Recupera tu contraseña - Cami-Cam CRM',
            html_content=f'''
            <h2>¿Olvidaste tu contraseña?</h2>
            <p>Hemos recibido una solicitud para restablecer tu contraseña.</p>
            <p>Haz clic en el siguiente enlace para crear una nueva contraseña:</p>
            <p><a href="{reset_url}" style="background-color: #3498db; color: white; padding: 12px 24px; text-decoration: none; border-radius: 4px; display: inline-block;">Restablecer Contraseña</a></p>
            <p>Este enlace expira en 1 hora.</p>
            <p>Si no solicitaste este cambio, ignora este email.</p>
            <hr>
            <p><small>Equipo Cami-Cam CRM</small></p>
            '''
        )
        
        sg = SendGridAPIClient(os.getenv('SENDGRID_API_KEY'))
        response = sg.send(message)
        print(f"✅ Email enviado a {email_destino} (Status: {response.status_code})")
        return True
        
    except Exception as e:
        print(f"❌ Error al enviar email: {str(e)}")
        return False
    

@app.route("/recuperar_password", methods=["POST"])
def recuperar_password():
    """
    Inicia el proceso de recuperación de contraseña.
    Genera un token temporal y lo almacena en la base de datos.
    """
    datos = request.json
    email = datos.get("email", "").strip().lower()
    
    if not email:
        return jsonify({"error": "Email es requerido"}), 400

    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no encontrado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cur = conn.cursor()
        # Verificar si el usuario existe
        cur.execute("SELECT id FROM users WHERE email = %s AND cliente_id = %s", (email, cliente_id))
        user = cur.fetchone()
        
        if not user:
            # No revelar si el email existe o no (seguridad)
            return jsonify({"mensaje": "Si el email existe, recibirás instrucciones"}), 200

        # Generar token de recuperación
        token = secrets.token_urlsafe(32)
        expiracion = datetime.utcnow() + timedelta(hours=1)  # Válido por 1 hora
        
        # Asegurar que las columnas existan
        cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token VARCHAR(100);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_expiracion TIMESTAMP;
        """)
        
        cur.execute("""
            UPDATE users 
            SET reset_token = %s, reset_expiracion = %s 
            WHERE email = %s AND cliente_id = %s
        """, (token, expiracion, email, cliente_id))
        
        conn.commit()
        
        # Generar URL de recuperación
        reset_url = f"https://{request.host}/restablecer_password?token={token}"
        
        # Enviar email real
        enviar_email_recuperacion(email, reset_url)
        
        return jsonify({"mensaje": "Si el email existe, recibirás instrucciones"}), 200
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en recuperar_password: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        

@app.route("/restablecer_password", methods=["GET", "POST"])
def restablecer_password():
    """
    Página para restablecer contraseña con token válido.
    """
    if request.method == "GET":
        token = request.args.get("token")
        if not token:
            return "Token inválido", 400
        
        # Verificar token
        cliente_id = obtener_cliente_id_de_subdominio()
        if not cliente_id:
            return "Cliente no encontrado", 404
            
        conn = conectar_db()
        if not conn:
            return "Error de conexión", 500
            
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id FROM users 
                WHERE reset_token = %s AND reset_expiracion > NOW() AND cliente_id = %s
            """, (token, cliente_id))
            
            if not cur.fetchone():
                return "Token inválido o expirado", 400
                
            return render_template_string("""
                <!DOCTYPE html>
                <html>
                <head><title>Restablecer Contraseña</title></head>
                <body>
                    <h2>Restablecer Contraseña</h2>
                    <form id="reset-form">
                        <input type="hidden" id="token" value="{{ token }}">
                        <input type="password" id="password1" placeholder="Nueva contraseña" required>
                        <input type="password" id="password2" placeholder="Confirmar contraseña" required>
                        <button type="submit">Restablecer</button>
                    </form>
                    <script>
                        document.getElementById('reset-form').addEventListener('submit', async (e) => {
                            e.preventDefault();
                            const token = document.getElementById('token').value;
                            const pass1 = document.getElementById('password1').value;
                            const pass2 = document.getElementById('password2').value;
                            
                            if (pass1 !== pass2) {
                                alert('Las contraseñas no coinciden');
                                return;
                            }
                            if (pass1.length < 6) {
                                alert('La contraseña debe tener al menos 6 caracteres');
                                return;
                            }
                            
                            const response = await fetch('/restablecer_password', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({token, password: pass1})
                            });
                            
                            if (response.ok) {
                                alert('Contraseña actualizada. Redirigiendo al login...');
                                setTimeout(() => window.location.href = '/login', 2000);
                            } else {
                                const data = await response.json();
                                alert(data.error || 'Error al restablecer');
                            }
                        });
                    </script>
                </body>
                </html>
            """, token=token)
            
        finally:
            liberar_db(conn)
    
    # POST: actualizar contraseña
    datos = request.json
    token = datos.get("token")
    password = datos.get("password")
    
    if not token or not password or len(password) < 6:
        return jsonify({"error": "Datos inválidos"}), 400
        
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no encontrado"}), 404
        
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500
        
    try:
        cur = conn.cursor()
        # Verificar token y actualizar contraseña
        nuevo_hash = generate_password_hash(password)
        cur.execute("""
            UPDATE users 
            SET password_hash = %s, reset_token = NULL, reset_expiracion = NULL
            WHERE reset_token = %s AND reset_expiracion > NOW() AND cliente_id = %s
        """, (nuevo_hash, token, cliente_id))
        
        if cur.rowcount == 0:
            return jsonify({"error": "Token inválido o expirado"}), 400
            
        conn.commit()
        return jsonify({"mensaje": "Contraseña actualizada"}), 200
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en restablecer_password POST: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"mensaje": "Sesión cerrada"}), 200
        
        

# 1) GET /users?tenant_id=...
@app.route("/users", methods=["GET"])
@requires_permission("view_users")
def listar_usuarios():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.email,
                ARRAY(
                    SELECT r.name
                    FROM user_roles ur
                    JOIN roles r ON ur.role_id = r.id
                    WHERE ur.user_id = u.id
                ) AS roles
            FROM users u
            WHERE u.cliente_id = %s
        """, (cliente_id,))
        rows = cur.fetchall()
        usuarios = [{"id": r[0], "email": r[1], "roles": r[2]} for r in rows]
        return jsonify(usuarios), 200
    finally:
        liberar_db(conn)
        
# 2) POST /users/invite
@app.route("/users/invite", methods=["POST"])
@requires_permission("manage_users")
def invitar_usuario():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    data = request.json or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"error": "email es requerido"}), 400

    # Generar contraseña temporal
    from uuid import uuid4
    from werkzeug.security import generate_password_hash
    temp_password = uuid4().hex[:8]
    pw_hash = generate_password_hash(temp_password)

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500

    try:
        cur = conn.cursor()
        # Crear usuario con cliente_id
        cur.execute("""
            INSERT INTO users (email, password_hash, cliente_id, activo)
            VALUES (%s, %s, %s, true)
            RETURNING id
        """, (email, pw_hash, cliente_id))
        user_id = cur.fetchone()[0]
        
        # Asignar rol 'seller'
        cur.execute("""
            INSERT INTO user_roles (user_id, role_id)
            SELECT %s, id FROM roles WHERE name = 'seller'
        """, (user_id,))
        conn.commit()
        
        # Enviar email (implementa tu función)
        # enviar_email(to=email, subject="Invitación", body=f"Contraseña: {temp_password}")
        
        return jsonify({"ok": True, "user_id": user_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
        
# 3) POST /users/<id>/roles
@app.route("/users/<int:user_id>/roles", methods=["POST"])
@requires_permission("manage_users")
def actualizar_roles(user_id):
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    # Verificar que el usuario pertenece al cliente actual
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500

    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE id = %s AND cliente_id = %s", (user_id, cliente_id))
        if not cur.fetchone():
            return jsonify({"error": "Usuario no encontrado"}), 404

        data = request.json or {}
        roles = data.get("roles")
        if not isinstance(roles, list):
            return jsonify({"error": "Se requiere un array 'roles'"}), 400

        # Borrar roles previos
        cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
        # Insertar nuevos roles
        for role_name in roles:
            cur.execute("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT %s, id FROM roles WHERE name = %s
            """, (user_id, role_name))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        

# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    
    
