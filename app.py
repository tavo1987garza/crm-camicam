import re
from dotenv import load_dotenv
import os
import hashlib
import tempfile
from urllib.parse import urlparse
load_dotenv()
import json
import traceback
from werkzeug.security import generate_password_hash, check_password_hash
import time
import base64
import uuid 
from datetime import datetime, timezone, date, timedelta
import requests
import boto3
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import secrets
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from functools import wraps
from flask import (
    Flask, request, jsonify, render_template, send_from_directory,
    current_app, redirect, url_for, session, g, abort, flash
)
from flask_socketio import SocketIO, join_room
from flask_cors import CORS




app = Flask(__name__)
REDIS_URL = os.getenv("REDIS_URL")
SOCKETIO_CHANNEL = "eventa_crm_socketio"

if not REDIS_URL:
    raise RuntimeError("Falta configurar REDIS_URL para Socket.IO")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    message_queue=REDIS_URL,
    channel=SOCKETIO_CHANNEL
)
app.secret_key = os.getenv('SECRET_KEY')




# ✅ CORS SIMPLIFICADO - Solo tu dominio principal
CORS(app, 
     resources={
         r"/calendario/checar_fecha": {
             "origins": [
                 "https://cami-cam.com",
                 "https://www.cami-cam.com"
             ],
             "supports_credentials": True,
             "methods": ["GET", "OPTIONS"],
             "allow_headers": ["Content-Type"]
         }
     },
     supports_credentials=True
)

# ============================================================================
# FUNCIONES DE ENCRIPTACIÓN PARA CREDENCIALES DE TENANTS
# ============================================================================
from cryptography.fernet import Fernet


# 🔑 Clave maestra para encriptar credenciales (cargada desde .env)
ENCRYPTION_KEY = os.getenv('CREDENTIALS_ENCRYPTION_KEY')

# Validar que la clave exista
if not ENCRYPTION_KEY:
    app.logger.error("❌ ERROR: CREDENTIALS_ENCRYPTION_KEY no está definida en .env")
    raise ValueError("CREDENTIALS_ENCRYPTION_KEY es requerida en .env")

# Inicializar cipher Fernet
cipher = Fernet(ENCRYPTION_KEY.encode())

def encriptar_credencial(valor):
    """
    Encripta una credencial sensible para guardar en BD.
    Retorna None si el valor está vacío.
    """
    if not valor or valor.strip() == "":
        return None
    try:
        return cipher.encrypt(valor.strip().encode()).decode()
    except Exception as e:
        app.logger.error(f"❌ Error al encriptar credencial: {str(e)}")
        return None

def desencriptar_credencial(valor_encriptado):
    """
    Desencripta una credencial para usarla (solo en memoria).
    Retorna None si el valor está vacío o es inválido.
    """
    if not valor_encriptado:
        return None
    try:
        return cipher.decrypt(valor_encriptado.encode()).decode()
    except Exception as e:
        app.logger.error(f"❌ Error al desencriptar credencial: {str(e)}")
        return None

# ============================================================================


@app.before_request
def cargar_usuario_actual():
    """
    Carga el usuario actual en g.current_user basado en la sesión y el cliente_id del subdominio.
    También maneja redirecciones especiales para subdominios.
    """
    # 🔁 Redirección especial para registro.eventa.com.mx
    if request.host == "registro.eventa.com.mx" and request.path == "/":
        return redirect("/registro")
    
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
    # 🔍 Detectar automáticamente si estamos en local o en producción
    es_local = "localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL
    
    # Si es local, desactiva SSL. Si es producción (DigitalOcean/Heroku), exígelo.
    modo_ssl = "disable" if es_local else "require"

    db_pool = pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        sslmode=modo_ssl  # <--- Ahora es dinámico e inteligente
    )
    app.logger.info(f"Pool de conexiones iniciado con éxito (SSL: {modo_ssl})")
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
    Soporta: eventa.com.mx y cami-cam.com (temporal)
    """
    host = request.host.lower()
    
    # Desarrollo
    if host == "localhost:5000" or host.startswith("127.0.0.1"):
        return 1

    # Soporte para ambos dominios durante la transición
    if host.endswith('.eventa.com.mx'):
        base_domain = 'eventa.com.mx'
        subdominio = host[:-len(base_domain)].rstrip('.')
    elif host.endswith('.cami-cam.com'):
        base_domain = 'cami-cam.com'
        subdominio = host[:-len(base_domain)].rstrip('.')
    else:
        # Dominio principal (eventa.com.mx o cami-cam.com)
        return None

    # Subdominios especiales
    if subdominio in ("www", "cotizador", "registro", ""):
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
@app.route("/calendario/checar_fecha", methods=["GET", "OPTIONS"])
def checar_fecha():
    # Manejar preflight OPTIONS
    if request.method == "OPTIONS":
        return jsonify({}), 200
    
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

# 📌 Obtener meta mensual
@app.route("/config/meta_mensual", methods=["GET"])
def obtener_meta_mensual():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"meta": 15}), 404
    
    conn = conectar_db()
    if not conn:
        return jsonify({"meta": 15}), 500
    
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT valor FROM config 
            WHERE clave = 'meta_mensual' AND cliente_id = %s
        """, (cliente_id,))
        row = cur.fetchone()
        meta = int(row[0]) if row and row[0].isdigit() else 15
        return jsonify({"meta": meta}), 200
    except Exception as e:
        print(f"❌ Error al obtener meta mensual: {str(e)}")
        return jsonify({"meta": 15}), 500
    finally:
        liberar_db(conn)

# 📌 Guardar meta mensual
@app.route("/config/meta_mensual", methods=["POST"])
def guardar_meta_mensual():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404
    
    data = request.json
    meta = data.get("meta")
    
    if not isinstance(meta, int) or meta < 1 or meta > 100:
        return jsonify({"error": "Meta debe ser un número entre 1 y 100"}), 400
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500
    
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO config (cliente_id, clave, valor)
            VALUES (%s, 'meta_mensual', %s)
            ON CONFLICT (cliente_id, clave)
            DO UPDATE SET valor = EXCLUDED.valor
        """, (cliente_id, str(meta)))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        print(f"❌ Error al guardar meta mensual: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    finally:
        liberar_db(conn)
        
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

# ============================================================================
# ESTADOS DE LEADS PERSONALIZABLES POR TENANT
# ============================================================================

@app.route("/leads/estados", methods=["GET"])
def obtener_estados_lead():
    """Obtener estados configurados por el tenant"""
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify([]), 401
    
    conn = conectar_db()
    if not conn:
        return jsonify([]), 500
    
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT nombre, color, orden, fijo
            FROM lead_estados_tenant
            WHERE cliente_id = %s AND activo = true
            ORDER BY orden ASC
        """, (cliente_id,))
        
        estados = [
            {
                "nombre": row[0],
                "color": row[1],
                "orden": row[2],
                "fijo": row[3]
            }
            for row in cur.fetchall()
        ]
        
        return jsonify(estados), 200
    except Exception as e:
        print(f"❌ Error en /leads/estados GET: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify([]), 500
    finally:
        liberar_db(conn)


@app.route("/leads/estados", methods=["POST"])
def guardar_estados_lead():
    """Guardar configuración de estados del tenant (ENFOQUE SIMPLIFICADO)"""
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    estados = request.json.get("estados", [])
    estados_eliminados = request.json.get("estados_eliminados", [])
    
    if not isinstance(estados, list):
        return jsonify({"error": "Formato inválido"}), 400
    
    # ✅ VALIDAR que exista al menos el estado fijo "✅ CONTACTO INICIAL"
    nombres_estados = [e.get("nombre", "").strip() for e in estados]
    if "✅ CONTACTO INICIAL" not in nombres_estados:
        return jsonify({"error": "El estado '✅ CONTACTO INICIAL' es requerido"}), 400
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar"}), 500
    
    try:
        cur = conn.cursor()
        
        # 🔹 1. MOVER LEADS de estados eliminados a "✅ CONTACTO INICIAL"
        leads_movidos = 0
        for estado_eliminado in estados_eliminados:
            cur.execute("""
                UPDATE leads SET estado = '✅ CONTACTO INICIAL'
                WHERE cliente_id = %s AND estado = %s
            """, (cliente_id, estado_eliminado))
            leads_movidos += cur.rowcount
        
        if leads_movidos > 0:
            print(f"✅ {leads_movidos} leads movidos a '✅ CONTACTO INICIAL'")
        
        # 🔹 2. ELIMINAR estados personalizados que ya no existen
        cur.execute("""
            DELETE FROM lead_estados_tenant 
            WHERE cliente_id = %s AND fijo = FALSE AND nombre NOT IN %s
        """, (cliente_id, tuple(nombres_estados)))
        
        # 🔹 3. INSERTAR/ACTUALIZAR estados personalizados (NO el fijo)
        for i, estado in enumerate(estados):
            nombre = estado.get("nombre", "").strip()
            color = estado.get("color", "#1e88e5")
            fijo = estado.get("fijo", False)
            
            if nombre:
                cur.execute("""
                    INSERT INTO lead_estados_tenant 
                    (cliente_id, nombre, color, orden, fijo, activo)
                    VALUES (%s, %s, %s, %s, %s, true)
                    ON CONFLICT (cliente_id, nombre) 
                    DO UPDATE SET color = EXCLUDED.color, orden = EXCLUDED.orden, fijo = EXCLUDED.fijo
                    -- ↑↑↑ IMPORTANTE: actualizar también 'orden' y 'fijo'
                """, (cliente_id, nombre, color, i, fijo))  # ← 'i' es el nuevo orden
        
        conn.commit()
        
        # 🔹 4. Emitir evento Socket
        socketio.emit(
            "configuracion_lead_actualizada",
            {
                "tipo": "estados",
                "cliente_id": cliente_id,
                "timestamp": datetime.now().isoformat(),
                "leads_movidos": leads_movidos
            },
            room=f"cliente_{cliente_id}"
        )
        
        return jsonify({"ok": True, "leads_movidos": leads_movidos}), 200
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en guardar_estados_lead: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


@app.route("/leads/estado/eliminar", methods=["POST"])
def eliminar_estado_lead():
    """Eliminar un estado personalizado (no fijo)"""
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    datos = request.json
    nombre_estado = datos.get("nombre")
    estado_destino = datos.get("estado_destino", "Contacto Inicial")
    
    if not nombre_estado:
        return jsonify({"error": "Falta nombre del estado"}), 400
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar"}), 500
    
    try:
        cur = conn.cursor()
        
        # Verificar que el estado no sea fijo
        cur.execute("""
            SELECT fijo FROM lead_estados_tenant 
            WHERE cliente_id = %s AND nombre = %s
        """, (cliente_id, nombre_estado))
        row = cur.fetchone()
        
        if not row:
            return jsonify({"error": "Estado no encontrado"}), 404
        
        if row[0]:  # Es fijo
            return jsonify({"error": "No se pueden eliminar estados fijos"}), 403
        
        # Mover leads a otro estado antes de eliminar
        cur.execute("""
            UPDATE leads SET estado = %s 
            WHERE cliente_id = %s AND estado = %s
        """, (estado_destino, cliente_id, nombre_estado))
        
        # Eliminar el estado
        cur.execute("""
            DELETE FROM lead_estados_tenant 
            WHERE cliente_id = %s AND nombre = %s
        """, (cliente_id, nombre_estado))
        
        conn.commit()
        
        # Emitir evento Socket
        socketio.emit(
            "configuracion_lead_actualizada",
            {
                "tipo": "estados",
                "cliente_id": cliente_id,
                "timestamp": datetime.now().isoformat()
            },
            room=f"cliente_{cliente_id}"
        )
        
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en eliminar_estado_lead: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


@app.route("/cambiar_estado_lead", methods=["POST"])
def cambiar_estado_lead():
    try:
        datos = request.json
        lead_id = datos.get("id")
        nuevo_estado = datos.get("estado")
        
        if not lead_id or not nuevo_estado:
            return jsonify({"error": "Faltan datos"}), 400
        
        cliente_id = obtener_cliente_id_de_subdominio()
        if not cliente_id:
            return jsonify({"error": "Cliente no autorizado"}), 404
        
        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión"}), 500
        
        cursor = conn.cursor()
        
        # ✅ VALIDAR que el estado existe para este tenant
        cursor.execute("""
            SELECT nombre FROM lead_estados_tenant 
            WHERE cliente_id = %s AND nombre = %s AND activo = true
        """, (cliente_id, nuevo_estado))
        
        if not cursor.fetchone():
            return jsonify({"error": "Estado no válido para este tenant"}), 400
        
        # Obtener el teléfono del lead para el evento
        cursor.execute("SELECT telefono FROM leads WHERE id = %s AND cliente_id = %s", (lead_id, cliente_id))
        row = cursor.fetchone()
        telefono = row[0] if row else None
        
        # Actualizar estado
        cursor.execute("UPDATE leads SET estado = %s WHERE id = %s AND cliente_id = %s", (nuevo_estado, lead_id, cliente_id))
        conn.commit()
        
        # Emitir evento en tiempo real
        if telefono:
            socketio.emit("lead_estado_actualizado", {
                "id": lead_id,
                "estado_nuevo": nuevo_estado,
                "telefono": telefono
            }, room=f"cliente_{cliente_id}")
        
        return jsonify({"mensaje": "Estado actualizado correctamente"}), 200
    except Exception as e:
        print(f"❌ Error en /cambiar_estado_lead: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)

# 📌 Validación de teléfono (debe tener 13 dígitos y empezar con 521 para México)
def validar_telefono(telefono):
    # Limpiamos espacios o guiones por si acaso
    telefono_limpio = str(telefono).replace(" ", "").replace("-", "")
    return len(telefono_limpio) == 13 and telefono_limpio.startswith("521")


# 📌 Crear un nuevo lead manualmente        
@app.route("/crear_lead", methods=["POST"])
def crear_lead():
    print(f"🔍 DEBUG crear_lead - Iniciando solicitud")
    
    cliente_id = obtener_cliente_id_de_subdominio()
    
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    datos = request.json
    nombre = datos.get("nombre")
    telefono = datos.get("telefono")
    notas = datos.get("notas", "")
    # ✅ RECIBIR estado del frontend con fallback al estado correcto
    estado = datos.get("estado", "✅ CONTACTO INICIAL")

    if not nombre or not telefono or not validar_telefono(telefono):
        return jsonify({"error": "El teléfono debe tener 13 dígitos"}), 400

    try:
        conn = conectar_db()
        if not conn:
            return jsonify({"error": "No se pudo conectar a la base de datos."}), 500
            
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leads (nombre, telefono, estado, notas, cliente_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (telefono, cliente_id) DO UPDATE
            SET notas = EXCLUDED.notas
            RETURNING id
        """, (nombre, telefono, estado, notas, cliente_id))  # ✅ Usar variable estado

        lead_id = cursor.fetchone()
        conn.commit()

        if lead_id:
            nuevo_lead = {
                "id": lead_id[0],
                "nombre": nombre,
                "telefono": telefono,
                "estado": estado,  # ✅ Enviar el estado correcto al frontend
                "notas": notas
            }
            socketio.emit(
                "nuevo_lead",
                nuevo_lead,
                room=f"cliente_{cliente_id}"
            )
            return jsonify({"mensaje": "Lead creado correctamente", "lead": nuevo_lead}), 200
        else:
            return jsonify({"mensaje": "No se pudo obtener el ID del lead"}), 500

    except Exception as e:
        print(f"💥 ERROR CRÍTICO crear_lead: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        liberar_db(conn)
          

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

        socketio.emit(
            "lead_eliminado",
            {"id": lead_id, "telefono": telefono},
            room=f"cliente_{cliente_id}"
        )
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




#'''''''''''''''''''''''''''''''''''''''''''''''
#------------SECION DE CHAT---------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
# ============================================================================
# FLOW ENGINE V2 - HELPERS MULTI-TENANT
# ============================================================================

def obtener_flow_tenant(cursor, cliente_id, flow_id):
    """
    Obtiene un flow exclusivamente dentro del tenant actual.
    Nunca buscar un flow solamente por ID.
    """
    cursor.execute("""
        SELECT
            id,
            cliente_id,
            nombre,
            descripcion,
            trigger_keyword,
            trigger_type,
            flow_data,
            timeout_segundos,
            max_reintentos,
            activo
        FROM bot_flows
        WHERE id = %s
          AND cliente_id = %s
          AND activo = TRUE
        LIMIT 1
    """, (flow_id, cliente_id))

    return cursor.fetchone()


def obtener_sesion_tenant(
    cursor,
    cliente_id,
    external_user_id,
    platform="whatsapp"
):
    """
    Obtiene exclusivamente la sesión perteneciente
    al tenant + usuario + plataforma.
    """
    cursor.execute("""
        SELECT
            id,
            cliente_id,
            external_user_id,
            platform,
            estado_actual,
            flujo_activo_id,
            current_node_id,
            waiting_node_id,
            waiting_variable,
            waiting_input_type,
            contexto,
            pending_variables,
            execution_meta,
            reintentos,
            ultimo_input_en,
            handoff_solicitado
        FROM conversation_sessions
        WHERE cliente_id = %s
          AND external_user_id = %s
          AND platform = %s
        LIMIT 1
    """, (
        cliente_id,
        external_user_id,
        platform
    ))

    return cursor.fetchone()


def obtener_lead_tenant(cursor, cliente_id, telefono):
    """
    Obtiene un lead exclusivamente dentro del tenant actual.
    """
    cursor.execute("""
        SELECT id, nombre, telefono, estado, contexto
        FROM leads
        WHERE cliente_id = %s
          AND telefono = %s
        LIMIT 1
    """, (
        cliente_id,
        telefono
    ))

    return cursor.fetchone()


def guardar_variable_lead(
    cursor,
    cliente_id,
    telefono,
    variable,
    valor
):
    """
    Guarda una variable permanente en leads.contexto.

    La combinación cliente_id + telefono evita cualquier
    contaminación entre tenants.
    """

    cursor.execute("""
        UPDATE leads
        SET contexto =
            COALESCE(contexto, '{}'::jsonb)
            || jsonb_build_object(%s, %s::jsonb)
        WHERE cliente_id = %s
          AND telefono = %s
    """, (
        variable,
        json.dumps(valor, ensure_ascii=False),
        cliente_id,
        telefono
    ))


def obtener_nodo(flow_data, node_id):
    if not isinstance(flow_data, dict):
        return None

    nodes = flow_data.get("nodes", [])

    for node in nodes:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node

    return None


def obtener_edges_salida(flow_data, node_id):
    if not isinstance(flow_data, dict):
        return []

    edges = flow_data.get("edges", [])

    return [
        edge
        for edge in edges
        if isinstance(edge, dict)
        and edge.get("source") == node_id
    ]


def obtener_siguiente_nodo_id(
    flow_data,
    node_id,
    source_handle="default"
):
    edges = obtener_edges_salida(flow_data, node_id)

    for edge in edges:
        handle = edge.get("source_handle", "default")

        if handle == source_handle:
            return edge.get("target")

    return None


def reemplazar_variables(texto, contexto):
    if not texto:
        return ""

    resultado = str(texto)

    if not isinstance(contexto, dict):
        return resultado

    for key, value in contexto.items():
        if value is not None:
            resultado = resultado.replace(
                f"{{{key}}}",
                str(value)
            )

    return resultado


# ============================================================================
# WHATSAPP MULTI-TENANT - RESOLUCIÓN SEGURA
# ============================================================================

def obtener_tenant_por_whatsapp_phone_id(cursor, whatsapp_phone_id):
    """
    Resuelve el tenant propietario de un WhatsApp Phone Number ID.

    IMPORTANTE:
    Nunca confiar en cliente_id enviado por Node.
    """

    if not whatsapp_phone_id:
        return None

    cursor.execute("""
        SELECT
            ti.cliente_id,
            ti.whatsapp_access_token,
            ti.whatsapp_phone_number_id,
            ti.bot_url
        FROM tenant_integraciones ti
        WHERE ti.whatsapp_phone_number_id = %s
        LIMIT 1
    """, (str(whatsapp_phone_id),))

    return cursor.fetchone()

def validar_bot_interno(request):
    secreto_configurado = os.getenv("BOT_INTERNAL_SECRET")
    secreto_recibido = request.headers.get("X-Bot-Secret")

    if not secreto_configurado:
        app.logger.error(
            "❌ BOT_INTERNAL_SECRET no está configurado"
        )
        return False

    return secreto_recibido == secreto_configurado


# ============================================================================
# ENVÍO WHATSAPP MULTI-TENANT: FLASK -> NODE -> META
# ============================================================================

def enviar_respuesta_whatsapp_tenant(
    cursor,
    cliente_id,
    telefono,
    respuesta
):
    """
    Envía una respuesta automática usando exclusivamente
    las credenciales WhatsApp del tenant actual.

    Soporta:
      - string -> texto
      - dict type=mensaje
      - dict type=imagen
      - dict type=video
      - dict type=opciones
    """

    # ------------------------------------------------------------------------
    # 1. Obtener integración del tenant
    # ------------------------------------------------------------------------
    cursor.execute("""
        SELECT
            whatsapp_access_token,
            whatsapp_phone_number_id,
            bot_url
        FROM tenant_integraciones
        WHERE cliente_id = %s
        LIMIT 1
    """, (cliente_id,))

    config = cursor.fetchone()

    if not config:
        raise Exception(
            f"No existe integración WhatsApp para cliente_id={cliente_id}"
        )

    token_encriptado, phone_id, bot_url = config

    if not token_encriptado:
        raise Exception(
            f"Tenant {cliente_id} no tiene whatsapp_access_token"
        )

    if not phone_id:
        raise Exception(
            f"Tenant {cliente_id} no tiene whatsapp_phone_number_id"
        )

    if not bot_url:
        raise Exception(
            f"Tenant {cliente_id} no tiene bot_url"
        )

    token = desencriptar_credencial(token_encriptado)

    if not token:
        raise Exception(
            f"No se pudo desencriptar el token del tenant {cliente_id}"
        )

    # ------------------------------------------------------------------------
    # 2. Verificar secreto interno Flask -> Node
    # ------------------------------------------------------------------------
    secreto_interno = os.getenv("BOT_INTERNAL_SECRET")

    if not secreto_interno:
        raise Exception(
            "BOT_INTERNAL_SECRET no está configurado en Flask"
        )

    headers = {
        "X-Bot-Secret": secreto_interno,
        "Content-Type": "application/json"
    }

    # ------------------------------------------------------------------------
    # 3. Keyword tradicional: respuesta tipo string
    # ------------------------------------------------------------------------
    if isinstance(respuesta, str):

        endpoint = f"{bot_url.rstrip('/')}/enviar_mensaje"

        payload = {
            "telefono": telefono,
            "mensaje": respuesta,
            "delay": 0,
            "whatsapp_token": token,
            "whatsapp_phone_id": phone_id
        }

    # ------------------------------------------------------------------------
    # 4. Respuesta estructurada del Flow Engine
    # ------------------------------------------------------------------------
    elif isinstance(respuesta, dict):

        tipo_respuesta = respuesta.get(
            "type",
            "mensaje"
        )

        caption = respuesta.get(
            "caption",
            ""
        )

        try:
            delay = int(
                respuesta.get("delay", 0) or 0
            )
        except (TypeError, ValueError):
            delay = 0

        # IMAGEN
        if tipo_respuesta == "imagen":

            url = respuesta.get("url", "")

            if not url:
                raise ValueError(
                    "Respuesta de imagen sin URL"
                )

            endpoint = f"{bot_url.rstrip('/')}/enviar_imagen"

            payload = {
                "telefono": telefono,
                "imageUrl": url,
                "caption": caption,
                "delay": delay,
                "whatsapp_token": token,
                "whatsapp_phone_id": phone_id
            }

        # VIDEO
        elif tipo_respuesta == "video":

            url = respuesta.get("url", "")

            if not url:
                raise ValueError(
                    "Respuesta de video sin URL"
                )

            endpoint = f"{bot_url.rstrip('/')}/enviar_video"

            payload = {
                "telefono": telefono,
                "videoUrl": url,
                "caption": caption,
                "delay": delay,
                "whatsapp_token": token,
                "whatsapp_phone_id": phone_id
            }

        # OPCIONES / BOTONES
        elif tipo_respuesta == "opciones":

            botones = respuesta.get(
                "bot_buttons",
                []
            )

            if not isinstance(botones, list) or not botones:
                raise ValueError(
                    "Respuesta de opciones sin botones"
                )

            endpoint = f"{bot_url.rstrip('/')}/enviar_botones"

            payload = {
                "telefono": telefono,
                "mensaje": caption or "Selecciona una opción:",
                "opciones": botones,
                "delay": delay,
                "whatsapp_token": token,
                "whatsapp_phone_id": phone_id
            }

        # TEXTO
        else:

            endpoint = f"{bot_url.rstrip('/')}/enviar_mensaje"

            payload = {
                "telefono": telefono,
                "mensaje": caption,
                "delay": delay,
                "whatsapp_token": token,
                "whatsapp_phone_id": phone_id
            }

    else:
        raise ValueError(
            "Formato de respuesta automática no soportado"
        )

    # ------------------------------------------------------------------------
    # 5. Enviar al gateway Node
    # ------------------------------------------------------------------------
    response = requests.post(
        endpoint,
        json=payload,
        headers=headers,
        timeout=15
    )

    if not response.ok:
        raise Exception(
            f"Node respondió {response.status_code}: {response.text}"
        )

    return True


# ============================================================================
# RECEPCIÓN DURABLE DE DESCRIPTORES MULTIMEDIA (BOT -> CRM)
# ============================================================================
@app.route("/recibir_media", methods=["POST"])
def recibir_media():
    if not validar_bot_interno(request):
        return jsonify({"error": "No autorizado"}), 401

    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "Descriptor multimedia inválido"}), 400

    campos_requeridos = (
        "whatsapp_phone_id",
        "meta_message_id",
        "remitente",
        "message_type",
        "media_id",
    )

    descriptor = {}
    for campo in campos_requeridos:
        valor = datos.get(campo)
        if not isinstance(valor, str) or not valor.strip():
            return jsonify({
                "error": f"Campo requerido inválido: {campo}"
            }), 400
        descriptor[campo] = valor.strip()

    tipos_permitidos = {
        "image",
        "video",
        "document",
        "audio",
        "sticker",
    }
    if descriptor["message_type"] not in tipos_permitidos:
        return jsonify({"error": "Tipo multimedia no soportado"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        tenant = obtener_tenant_por_whatsapp_phone_id(
            cursor,
            descriptor["whatsapp_phone_id"]
        )

        if not tenant:
            conn.rollback()
            return jsonify({"error": "Número de WhatsApp no registrado"}), 404

        cliente_id, _, tenant_phone_id, _ = tenant

        # Defensa en profundidad: el Phone ID retornado por la integración debe
        # coincidir exactamente con el descriptor normalizado recibido de Node.
        if str(tenant_phone_id).strip() != descriptor["whatsapp_phone_id"]:
            conn.rollback()
            app.logger.error(
                "Inconsistencia de Phone ID al reservar evento multimedia "
                f"para cliente_id={cliente_id}"
            )
            return jsonify({"error": "Identidad de WhatsApp inconsistente"}), 409

        cursor.execute("""
            INSERT INTO whatsapp_inbound_events (
                cliente_id,
                meta_message_id,
                whatsapp_phone_number_id,
                remitente,
                message_type,
                media_id,
                status,
                attempts,
                next_attempt_at,
                creado_en,
                actualizado_en
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, NOW(), NOW(), NOW())
            ON CONFLICT (cliente_id, meta_message_id)
            DO NOTHING
            RETURNING id
        """, (
            cliente_id,
            descriptor["meta_message_id"],
            descriptor["whatsapp_phone_id"],
            descriptor["remitente"],
            descriptor["message_type"],
            descriptor["media_id"],
        ))

        nueva_fila = cursor.fetchone()
        if nueva_fila:
            event_id = nueva_fila[0]
            conn.commit()
            app.logger.info(
                "Evento multimedia aceptado: "
                f"cliente_id={cliente_id}, event_id={event_id}, "
                f"message_type={descriptor['message_type']}"
            )
            return jsonify({
                "ok": True,
                "status": "accepted",
                "event_id": event_id,
            }), 202

        cursor.execute("""
            SELECT id, status
            FROM whatsapp_inbound_events
            WHERE cliente_id = %s
              AND meta_message_id = %s
        """, (
            cliente_id,
            descriptor["meta_message_id"],
        ))
        evento_existente = cursor.fetchone()
        conn.rollback()

        if not evento_existente:
            app.logger.error(
                "No se pudo recuperar un evento multimedia duplicado "
                f"para cliente_id={cliente_id}"
            )
            return jsonify({"error": "No se pudo recuperar el evento"}), 500

        event_id, estado = evento_existente
        respuestas_duplicado = {
            "completed": ("already_processed", 200),
            "pending": ("already_accepted", 202),
            "processing": ("already_processing", 202),
            "failed": ("previously_failed", 200),
        }
        estado_respuesta, codigo_http = respuestas_duplicado.get(
            estado,
            ("unknown_status", 409)
        )

        return jsonify({
            "ok": estado in respuestas_duplicado,
            "status": estado_respuesta,
            "event_id": event_id,
        }), codigo_http

    except Exception as e:
        conn.rollback()
        app.logger.error(
            f"Error reservando evento multimedia: {type(e).__name__}"
        )
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        liberar_db(conn)


def resolver_identidad_socket():
    """Resuelve usuario y tenant del socket sin confiar en datos del cliente."""
    user_id = session.get("user_id")
    if not user_id:
        return None

    cliente_id_subdominio = obtener_cliente_id_de_subdominio()
    if not cliente_id_subdominio:
        return None

    conn = conectar_db()
    if not conn:
        return None

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, cliente_id
            FROM users
            WHERE id = %s
              AND cliente_id = %s
              AND activo = true
        """, (user_id, cliente_id_subdominio))
        usuario = cursor.fetchone()
        if not usuario:
            return None

        return {
            "user_id": usuario[0],
            "cliente_id": usuario[1]
        }
    finally:
        liberar_db(conn)


@socketio.on("connect")
def conectar_socket(auth=None):
    try:
        identidad = resolver_identidad_socket()
    except Exception as e:
        app.logger.error(
            "Error resolviendo identidad Socket.IO: "
            f"tipo_error={type(e).__name__}"
        )
        return False

    if not identidad:
        app.logger.warning("Conexión Socket.IO rechazada por identidad inválida")
        return False

    room = f"cliente_{identidad['cliente_id']}"
    join_room(room)
    app.logger.info(
        "Socket.IO autenticado: "
        f"user_id={identidad['user_id']}, "
        f"cliente_id={identidad['cliente_id']}, "
        f"room={room}"
    )


# ============================================================================
# WORKER MULTIMEDIA: PROCESAMIENTO MANUAL DE UN TRABAJO
# ============================================================================
class ErrorProcesamientoMedia(Exception):
    """Error seguro para persistir sin secretos ni URLs temporales."""


WHATSAPP_MEDIA_LIMITES_DEFAULT = {
    "image": 5 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "document": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "sticker": 500 * 1024,
}

WHATSAPP_MEDIA_ENV_LIMITES = {
    "image": "WHATSAPP_MEDIA_MAX_IMAGE_BYTES",
    "video": "WHATSAPP_MEDIA_MAX_VIDEO_BYTES",
    "document": "WHATSAPP_MEDIA_MAX_DOCUMENT_BYTES",
    "audio": "WHATSAPP_MEDIA_MAX_AUDIO_BYTES",
    "sticker": "WHATSAPP_MEDIA_MAX_STICKER_BYTES",
}

WHATSAPP_MEDIA_MIME_EXTENSIONES = {
    "image": {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    },
    "video": {
        "video/mp4": "mp4",
        "video/3gpp": "3gp",
    },
    "document": {
        "application/pdf": "pdf",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-powerpoint": "ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "text/plain": "txt",
    },
    "audio": {
        "audio/aac": "aac",
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
        "audio/amr": "amr",
        "audio/ogg": "ogg",
        "audio/opus": "opus",
    },
    "sticker": {
        "image/webp": "webp",
    },
}


def _obtener_entero_positivo_env(nombre, valor_default):
    valor = os.getenv(nombre)
    if valor is None or not valor.strip():
        return valor_default
    try:
        entero = int(valor)
    except (TypeError, ValueError):
        raise ErrorProcesamientoMedia(f"configuracion_invalida:{nombre}")
    if entero <= 0:
        raise ErrorProcesamientoMedia(f"configuracion_invalida:{nombre}")
    return entero


def _limite_media_bytes(message_type):
    if message_type not in WHATSAPP_MEDIA_LIMITES_DEFAULT:
        raise ErrorProcesamientoMedia("message_type_no_soportado")
    return _obtener_entero_positivo_env(
        WHATSAPP_MEDIA_ENV_LIMITES[message_type],
        WHATSAPP_MEDIA_LIMITES_DEFAULT[message_type]
    )


def _normalizar_mime(valor):
    if not isinstance(valor, str):
        return None
    mime = valor.split(";", 1)[0].strip().lower()
    return mime or None


def _resolver_mime_y_extension(message_type, mime_meta, mime_descarga):
    mime_meta = _normalizar_mime(mime_meta)
    mime_descarga = _normalizar_mime(mime_descarga)
    genericos = {None, "application/octet-stream", "binary/octet-stream"}

    if (
        mime_meta not in genericos
        and mime_descarga not in genericos
        and mime_meta != mime_descarga
    ):
        raise ErrorProcesamientoMedia("mime_inconsistente")

    mime = (
        mime_meta if mime_meta not in genericos
        else mime_descarga
    )
    extensiones = WHATSAPP_MEDIA_MIME_EXTENSIONES.get(message_type, {})
    extension = extensiones.get(mime)
    if not mime or not extension:
        raise ErrorProcesamientoMedia("mime_no_soportado")
    return mime, extension


def reclamar_trabajo_multimedia():
    conn = conectar_db()
    if not conn:
        raise ErrorProcesamientoMedia("db_no_disponible_claim")

    lock_token = str(uuid.uuid4())
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            WITH candidato AS (
                SELECT id
                FROM whatsapp_inbound_events
                WHERE (
                    status = 'pending'
                    OR (
                        status = 'failed'
                        AND next_attempt_at IS NOT NULL
                    )
                )
                  AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at ASC, creado_en ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE whatsapp_inbound_events AS evento
            SET status = 'processing',
                attempts = evento.attempts + 1,
                locked_at = NOW(),
                lock_token = %s,
                actualizado_en = NOW()
            FROM candidato
            WHERE evento.id = candidato.id
            RETURNING
                evento.id AS event_id,
                evento.cliente_id,
                evento.whatsapp_phone_number_id,
                evento.remitente,
                evento.message_type,
                evento.media_id,
                evento.attempts,
                evento.lock_token,
                evento.creado_en AS evento_creado_en
        """, (lock_token,))
        trabajo = cursor.fetchone()
        conn.commit()
        return dict(trabajo) if trabajo else None
    except Exception:
        conn.rollback()
        raise
    finally:
        liberar_db(conn)


def recuperar_leases_multimedia_vencidos():
    timeout_segundos = _obtener_entero_positivo_env(
        "WHATSAPP_MEDIA_LEASE_TIMEOUT_SECONDS",
        15 * 60
    )
    conn = conectar_db()
    if not conn:
        raise ErrorProcesamientoMedia("db_no_disponible_recovery")

    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_inbound_events
            SET status = 'pending',
                locked_at = NULL,
                lock_token = NULL,
                next_attempt_at = NOW(),
                last_error = 'lease_expirado',
                actualizado_en = NOW()
            WHERE status = 'processing'
              AND locked_at < NOW() - (%s * INTERVAL '1 second')
        """, (timeout_segundos,))
        recuperados = cursor.rowcount
        conn.commit()
        return recuperados
    except Exception:
        conn.rollback()
        raise
    finally:
        liberar_db(conn)


def _obtener_integracion_media(trabajo):
    conn = conectar_db()
    if not conn:
        raise ErrorProcesamientoMedia("db_no_disponible_integracion")

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT whatsapp_access_token, whatsapp_phone_number_id
            FROM tenant_integraciones
            WHERE cliente_id = %s
              AND whatsapp_phone_number_id = %s
        """, (
            trabajo["cliente_id"],
            trabajo["whatsapp_phone_number_id"],
        ))
        integracion = cursor.fetchone()
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        liberar_db(conn)

    if not integracion or not integracion[0]:
        raise ErrorProcesamientoMedia("integracion_whatsapp_invalida")

    token = desencriptar_credencial(integracion[0])
    if not token or not token.strip():
        raise ErrorProcesamientoMedia("token_whatsapp_invalido")

    phone_id = str(integracion[1]).strip() if integracion[1] else ""
    if phone_id != str(trabajo["whatsapp_phone_number_id"]).strip():
        raise ErrorProcesamientoMedia("phone_id_inconsistente")

    return token


def _obtener_metadata_meta(media_id, token):
    version = os.getenv("WABA_VERSION", "v21.0").strip()
    endpoint = f"https://graph.facebook.com/{version}/{media_id}"
    try:
        respuesta = requests.get(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=(5, 20)
        )
    except requests.RequestException:
        raise ErrorProcesamientoMedia("meta_metadata_network_error")

    try:
        if respuesta.status_code != 200:
            raise ErrorProcesamientoMedia(
                f"meta_metadata_http_{respuesta.status_code}"
            )

        try:
            metadata = respuesta.json()
        except ValueError:
            raise ErrorProcesamientoMedia("meta_metadata_json_invalido")
    finally:
        respuesta.close()

    url_temporal = metadata.get("url")
    if not isinstance(url_temporal, str) or not url_temporal.strip():
        raise ErrorProcesamientoMedia("meta_media_url_ausente")
    if urlparse(url_temporal).scheme.lower() != "https":
        raise ErrorProcesamientoMedia("meta_media_url_no_https")

    return {
        "url": url_temporal.strip(),
        "mime_type": metadata.get("mime_type"),
        "file_size": metadata.get("file_size"),
    }


def _descargar_media_a_temporal(metadata, token, message_type):
    limite_bytes = _limite_media_bytes(message_type)
    file_size_meta = metadata.get("file_size")
    if file_size_meta is not None:
        try:
            if int(file_size_meta) > limite_bytes:
                raise ErrorProcesamientoMedia("media_excede_limite_metadata")
        except (TypeError, ValueError):
            raise ErrorProcesamientoMedia("media_file_size_invalido")

    try:
        respuesta = requests.get(
            metadata["url"],
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=(5, 60)
        )
    except requests.RequestException:
        raise ErrorProcesamientoMedia("meta_download_network_error")

    archivo = None
    entregar_archivo = False
    try:
        if respuesta.status_code != 200:
            raise ErrorProcesamientoMedia(
                f"meta_download_http_{respuesta.status_code}"
            )

        if urlparse(respuesta.url).scheme.lower() != "https":
            raise ErrorProcesamientoMedia("meta_download_redirect_no_https")

        mime_type, extension = _resolver_mime_y_extension(
            message_type,
            metadata.get("mime_type"),
            respuesta.headers.get("Content-Type")
        )

        content_length = respuesta.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > limite_bytes:
                    raise ErrorProcesamientoMedia("media_excede_limite_header")
            except ValueError:
                raise ErrorProcesamientoMedia("content_length_invalido")

        archivo = tempfile.TemporaryFile(mode="w+b")
        sha256 = hashlib.sha256()
        size_bytes = 0
        for bloque in respuesta.iter_content(chunk_size=64 * 1024):
            if not bloque:
                continue
            size_bytes += len(bloque)
            if size_bytes > limite_bytes:
                raise ErrorProcesamientoMedia("media_excede_limite_stream")
            sha256.update(bloque)
            archivo.write(bloque)

        if size_bytes <= 0:
            raise ErrorProcesamientoMedia("media_vacia")

        archivo.seek(0)
        resultado = {
            "archivo": archivo,
            "mime_type": mime_type,
            "extension": extension,
            "size_bytes": size_bytes,
            "sha256": sha256.hexdigest(),
        }
        entregar_archivo = True
        return resultado
    finally:
        try:
            respuesta.close()
        finally:
            if archivo is not None and not entregar_archivo:
                archivo.close()


def _crear_cliente_s3_media():
    region = os.getenv("AWS_REGION")
    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    bucket = os.getenv("S3_BUCKET_NAME")
    if not all((region, access_key, secret_key, bucket)):
        raise ErrorProcesamientoMedia("configuracion_s3_incompleta")

    cliente = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    return cliente, bucket.strip()


def _marcar_evento_fallido(trabajo, error_seguro):
    attempts = int(trabajo.get("attempts") or 1)
    max_attempts = _obtener_entero_positivo_env(
        "WHATSAPP_MEDIA_MAX_ATTEMPTS",
        5
    )
    programar_retry = attempts < max_attempts
    backoff_segundos = min(60 * 60, 30 * (2 ** max(0, attempts - 1)))
    error_truncado = str(error_seguro)[:500]

    conn = conectar_db()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_inbound_events
            SET status = 'failed',
                locked_at = NULL,
                lock_token = NULL,
                last_error = %s,
                next_attempt_at = CASE
                    WHEN %s THEN NOW() + (%s * INTERVAL '1 second')
                    ELSE NULL
                END,
                actualizado_en = NOW()
            WHERE id = %s
              AND cliente_id = %s
              AND status = 'processing'
              AND lock_token = %s
        """, (
            error_truncado,
            programar_retry,
            backoff_segundos,
            trabajo["event_id"],
            trabajo["cliente_id"],
            str(trabajo["lock_token"]),
        ))
        actualizado = cursor.rowcount == 1
        conn.commit()
        return actualizado
    except Exception:
        conn.rollback()
        return False
    finally:
        liberar_db(conn)


def _persistir_media_y_completar(trabajo, datos_media, bucket, s3_key):
    conn = conectar_db()
    if not conn:
        raise ErrorProcesamientoMedia("db_no_disponible_finalize")

    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO whatsapp_media (
                cliente_id,
                inbound_event_id,
                media_type,
                meta_media_id,
                s3_bucket,
                s3_key,
                mime_type,
                size_bytes,
                sha256,
                original_filename,
                creado_en
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NOW())
            RETURNING id
        """, (
            trabajo["cliente_id"],
            trabajo["event_id"],
            trabajo["message_type"],
            trabajo["media_id"],
            bucket,
            s3_key,
            datos_media["mime_type"],
            datos_media["size_bytes"],
            datos_media["sha256"],
        ))
        media_id_db = cursor.fetchone()[0]

        tipos_mensaje = {
            "image": ("[Imagen]", "recibido_imagen"),
            "video": ("[Video]", "recibido_video"),
        }
        datos_mensaje = tipos_mensaje.get(trabajo["message_type"])
        if not datos_mensaje:
            raise ErrorProcesamientoMedia("message_type_sin_mensaje_soportado")

        texto_mensaje, tipo_mensaje = datos_mensaje
        cursor.execute("""
            INSERT INTO mensajes (
                plataforma,
                remitente,
                mensaje,
                estado,
                tipo,
                cliente_id,
                fecha,
                whatsapp_media_id
            )
            VALUES ('whatsapp', %s, %s, 'Nuevo', %s, %s, %s, %s)
            ON CONFLICT (whatsapp_media_id)
                WHERE whatsapp_media_id IS NOT NULL
            DO NOTHING
            RETURNING id
        """, (
            trabajo["remitente"],
            texto_mensaje,
            tipo_mensaje,
            trabajo["cliente_id"],
            trabajo["evento_creado_en"],
            media_id_db,
        ))
        mensaje_insertado = cursor.fetchone()

        if mensaje_insertado:
            mensaje_id_db = mensaje_insertado[0]
        else:
            cursor.execute("""
                SELECT id
                FROM mensajes
                WHERE whatsapp_media_id = %s
                  AND cliente_id = %s
            """, (media_id_db, trabajo["cliente_id"]))
            mensaje_existente = cursor.fetchone()
            if not mensaje_existente:
                raise ErrorProcesamientoMedia("mensaje_media_no_resuelto")
            mensaje_id_db = mensaje_existente[0]

        cursor.execute("""
            UPDATE whatsapp_inbound_events
            SET status = 'completed',
                processed_at = NOW(),
                locked_at = NULL,
                lock_token = NULL,
                last_error = NULL,
                actualizado_en = NOW()
            WHERE id = %s
              AND cliente_id = %s
              AND status = 'processing'
              AND lock_token = %s
        """, (
            trabajo["event_id"],
            trabajo["cliente_id"],
            str(trabajo["lock_token"]),
        ))
        if cursor.rowcount != 1:
            raise ErrorProcesamientoMedia("lease_perdido_finalize")

        conn.commit()
        return {
            "media_id": media_id_db,
            "mensaje_id": mensaje_id_db,
            "mensaje": texto_mensaje,
            "tipo": tipo_mensaje,
            "fecha": trabajo["evento_creado_en"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        liberar_db(conn)


def procesar_un_trabajo_multimedia():
    trabajo = reclamar_trabajo_multimedia()
    if not trabajo:
        return {"status": "no_job"}

    archivo_temporal = None
    cliente_s3 = None
    bucket = None
    s3_key = None
    objeto_subido = False

    try:
        if trabajo["message_type"] not in WHATSAPP_MEDIA_MIME_EXTENSIONES:
            raise ErrorProcesamientoMedia("message_type_no_soportado")

        token = _obtener_integracion_media(trabajo)
        metadata = _obtener_metadata_meta(trabajo["media_id"], token)
        datos_media = _descargar_media_a_temporal(
            metadata,
            token,
            trabajo["message_type"]
        )
        archivo_temporal = datos_media["archivo"]

        cliente_s3, bucket = _crear_cliente_s3_media()
        s3_key = (
            f"tenants/{trabajo['cliente_id']}/whatsapp/inbound/"
            f"{trabajo['message_type']}/{uuid.uuid4()}."
            f"{datos_media['extension']}"
        )
        try:
            cliente_s3.upload_fileobj(
                archivo_temporal,
                bucket,
                s3_key,
                ExtraArgs={"ContentType": datos_media["mime_type"]}
            )
        except Exception:
            raise ErrorProcesamientoMedia("s3_upload_error")
        objeto_subido = True

        resultado_persistencia = _persistir_media_y_completar(
            trabajo,
            datos_media,
            bucket,
            s3_key
        )
        media_id_db = resultado_persistencia["media_id"]

        fecha_socket = resultado_persistencia["fecha"]
        if hasattr(fecha_socket, "isoformat"):
            fecha_socket = fecha_socket.isoformat()

        try:
            socketio.emit(
                "nuevo_mensaje",
                {
                    "remitente": trabajo["remitente"],
                    "mensaje": resultado_persistencia["mensaje"],
                    "tipo": resultado_persistencia["tipo"],
                    "fecha": fecha_socket,
                    "cliente_id": trabajo["cliente_id"],
                    "whatsapp_media_id": media_id_db,
                    "media_url": f"/api/media/{media_id_db}",
                },
                room=f"cliente_{trabajo['cliente_id']}"
            )
        except Exception as e:
            app.logger.warning(
                "No se pudo emitir media completada por Socket.IO: "
                f"cliente_id={trabajo['cliente_id']}, "
                f"event_id={trabajo['event_id']}, "
                f"tipo_error={type(e).__name__}"
            )

        app.logger.info(
            "Media completada: "
            f"cliente_id={trabajo['cliente_id']}, "
            f"event_id={trabajo['event_id']}, "
            f"media_type={trabajo['message_type']}, "
            f"attempts={trabajo['attempts']}, "
            f"size_bytes={datos_media['size_bytes']}"
        )
        return {
            "status": "completed",
            "event_id": trabajo["event_id"],
            "media_id": media_id_db,
        }

    except Exception as e:
        if objeto_subido and cliente_s3 and bucket and s3_key:
            try:
                cliente_s3.delete_object(Bucket=bucket, Key=s3_key)
            except Exception:
                app.logger.error(
                    "No se pudo limpiar objeto S3 huérfano: "
                    f"cliente_id={trabajo['cliente_id']}, "
                    f"event_id={trabajo['event_id']}"
                )

        error_seguro = (
            str(e)
            if isinstance(e, ErrorProcesamientoMedia)
            else f"error_interno:{type(e).__name__}"
        )
        actualizado = _marcar_evento_fallido(trabajo, error_seguro)
        app.logger.error(
            "Media fallida: "
            f"cliente_id={trabajo['cliente_id']}, "
            f"event_id={trabajo['event_id']}, "
            f"media_type={trabajo['message_type']}, "
            f"attempts={trabajo['attempts']}, "
            f"estado_actualizado={actualizado}"
        )
        return {
            "status": "failed",
            "event_id": trabajo["event_id"],
            "error": error_seguro,
        }

    finally:
        if archivo_temporal:
            archivo_temporal.close()


@app.cli.command("procesar-media-una-vez")
def procesar_media_una_vez_command():
    """Procesa como máximo un trabajo multimedia y termina."""
    resultado = procesar_un_trabajo_multimedia()
    print(json.dumps(resultado, ensure_ascii=False))


# ============================================================================
# 1. RECIBIR MENSAJES DESDE WHATSAPP (BOT -> CRM)
# ============================================================================
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    # 🔥 LATIDO
    print(
        f"🔥 [CRM] ¡Petición recibida en /recibir_mensaje! "
        f"Payload: {request.json}"
    )

    # 🔐 Solo el Bot puede llamar este endpoint
    if not validar_bot_interno(request):
        print("⛔ [CRM] Petición rechazada: BOT_INTERNAL_SECRET inválido")
        return jsonify({"error": "No autorizado"}), 401

    datos = request.get_json(silent=True) or {}

    plataforma = str(
        datos.get("plataforma", "whatsapp")
    ).lower().strip()

    remitente = str(
        datos.get("remitente", "")
    ).strip()

    mensaje = datos.get("mensaje")

    tipo = str(
        datos.get("tipo", "recibido")
    ).lower().strip()

    whatsapp_phone_id = str(
        datos.get("whatsapp_phone_id", "")
    ).strip()

    print(
        f"📝 [CRM] Datos procesados -> "
        f"Remitente: {remitente}, "
        f"Mensaje: '{mensaje}', "
        f"Tipo: '{tipo}', "
        f"WhatsApp Phone ID: '{whatsapp_phone_id}'"
    )

    if not remitente or mensaje is None:
        return jsonify({
            "error": "Faltan datos (remitente o mensaje)"
        }), 400

    if plataforma == "whatsapp" and not whatsapp_phone_id:
        return jsonify({
            "error": "Falta whatsapp_phone_id"
        }), 400

    tipos_validos = {
        "enviado",
        "recibido",
        "recibido_imagen",
        "enviado_imagen",
        "recibido_video",
        "enviado_video"
    }

    # 🔄 Normalización
    if tipo in ["recibido_image", "enviado_image"]:
        tipo = tipo.replace("image", "imagen")
    elif tipo in ["recibido_video", "enviado_video"]:
        tipo = tipo.replace("video", "video")

    if tipo not in tipos_validos:
        tipo = "recibido"

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

# ============================================================================
# 🔐 RESOLVER TENANT REAL DESDE EL WHATSAPP PHONE NUMBER ID
# ============================================================================

        tenant = obtener_tenant_por_whatsapp_phone_id(
            cursor,
            whatsapp_phone_id
        )

        if not tenant:
            print(
                f"⛔ [CRM] WhatsApp Phone ID no registrado: "
                f"{whatsapp_phone_id}"
            )

            return jsonify({
                "error": "Número de WhatsApp no registrado"
            }), 404

        (
            cliente_id,
            access_token_encriptado,
            tenant_phone_id,
            bot_url
        ) = tenant

        print(
            f"✅ [CRM] Tenant resuelto correctamente -> "
            f"cliente_id={cliente_id}, "
            f"phone_id={tenant_phone_id}"
        )

        # 1. Verificar si el lead ya existe PARA ESTE CLIENTE
        cursor.execute("SELECT id, nombre FROM leads WHERE telefono = %s AND cliente_id = %s", (remitente, cliente_id))
        lead = cursor.fetchone()
        
        if not lead:
            nombre_por_defecto = f"Lead {remitente[-4:]}"
            cursor.execute("""
                INSERT INTO leads (nombre, telefono, estado, cliente_id)
                VALUES (%s, %s, '✅ CONTACTO INICIAL', %s)
                ON CONFLICT (telefono, cliente_id) DO NOTHING
                RETURNING id
            """, (nombre_por_defecto, remitente, cliente_id))
            row = cursor.fetchone()
            lead_id = row[0] if row else None
        else:
            lead_id = lead[0]

        # 2. Guardar el mensaje en la BD
        cursor.execute("""
            INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo, cliente_id, fecha)
            VALUES (%s, %s, %s, 'Nuevo', %s, %s, NOW())
        """, (plataforma, remitente, mensaje, tipo, cliente_id))

        # Hacer durable y publicar el inbound antes de cualquier automatización.
        conn.commit()
        socketio.emit("nuevo_mensaje", {
            "remitente": remitente,
            "mensaje": mensaje,
            "tipo": tipo,
            "fecha": datetime.now().isoformat(),
            "cliente_id": cliente_id
        }, room=f"cliente_{cliente_id}")

        # ========================================================================
        # 🧠 3. LÓGICA DE RESPUESTA AUTOMÁTICA (FLUJOS + TUS KEYWORDS FUNCIONALES)
        # ========================================================================
        bot_response = None
        keyword_id_usada = None
        nivel_match = None
        
        if tipo == "recibido" and mensaje and mensaje.strip():
            try:
                mensaje_limpio = mensaje.strip()
                flujo_activo_encontrado = False

                # ==========================================
                # PASO 1: ¿El usuario ya está en un FLUJO activo?
                # ==========================================
                cursor.execute("""
                    SELECT id, flujo_activo_id, paso_actual, contexto 
                    FROM conversation_sessions 
                    WHERE cliente_id = %s AND external_user_id = %s AND platform = 'whatsapp' AND estado_actual = 'active'
                """, (cliente_id, remitente))
                sesion = cursor.fetchone()
                
                if sesion:
                    sesion_id, flujo_id, paso_actual, contexto = sesion
                    contexto_dict = contexto if isinstance(contexto, dict) else {}
                    
                    cursor.execute("SELECT nombre, pasos FROM bot_flows WHERE id = %s AND activo = true", (flujo_id,))
                    flujo = cursor.fetchone()
                    
                    if flujo:
                        nombre_flujo, pasos_json = flujo
                        pasos = pasos_json if isinstance(pasos_json, list) else []
                        
                        # 1. Guardar respuesta en contexto si el paso anterior lo pedía
                        if paso_actual > 0 and paso_actual <= len(pasos):
                            paso_anterior = pasos[paso_actual - 1]
                            if paso_anterior.get("tipo") == "opciones" and "campo" in paso_anterior:
                                contexto_dict[paso_anterior["campo"]] = mensaje_limpio
                            elif paso_anterior.get("tipo") == "pregunta" and "campo" in paso_anterior:
                                contexto_dict[paso_anterior["campo"]] = mensaje_limpio
                        
                  
                        # 2. Obtener el siguiente paso (con protección contra nulos)
                        if paso_actual < len(pasos):
                            siguiente_paso = pasos[paso_actual]
                            
                            # 🛡️ SEGURIDAD: Si el paso es None o no es un diccionario, reiniciar flujo
                            if not siguiente_paso or not isinstance(siguiente_paso, dict):
                                print(f"⚠️ [FLUJO] El paso {paso_actual} es nulo o inválido. Reiniciando flujo.")
                                cursor.execute("""
                                    UPDATE conversation_sessions 
                                    SET estado_actual = 'idle', flujo_activo_id = NULL, paso_actual = 0, contexto = '{}'::jsonb
                                    WHERE id = %s
                                """, (sesion_id,))
                                conn.commit()
                                # No enviar respuesta de flujo, dejar que las keywords normales actúen
                                bot_response = None
                                flujo_activo_encontrado = False
                            else:
                                tipo_paso = siguiente_paso.get("tipo", "mensaje")
                                
                                # 🎯 Reemplazar variables dinámicas en el texto (ej: {tipo_consulta})
                                texto_base = siguiente_paso.get("texto", "")
                                for key, value in contexto_dict.items():
                                    texto_base = texto_base.replace(f"{{{key}}}", str(value))
                                
                                # 🎯 Construir el payload estructurado para el Bot
                                respuesta_a_enviar = {
                                    "type": tipo_paso,
                                    "caption": texto_base,
                                    "bot_buttons": []
                                }
                                
                                # Si es imagen o video, agregar la URL
                                if tipo_paso in ["imagen", "video"]:
                                    respuesta_a_enviar["url"] = siguiente_paso.get("url", "")
                                
                                # Si es opciones, preparar los botones (máx 3)
                                if tipo_paso == "opciones":
                                    opciones = siguiente_paso.get("opciones", [])
                                    respuesta_a_enviar["bot_buttons"] = opciones[:3]
                                    if not respuesta_a_enviar["caption"]:
                                        respuesta_a_enviar["caption"] = "Por favor, selecciona una opción:"
                                
                                bot_response = respuesta_a_enviar
                                nivel_match = f"FLUJO: {nombre_flujo} (Paso {paso_actual + 1})"
                                flujo_activo_encontrado = True
                                
                                # 3. Actualizar la sesión
                                cursor.execute("""
                                    UPDATE conversation_sessions 
                                    SET paso_actual = %s, contexto = %s, ultimo_input_en = NOW()
                                    WHERE id = %s
                                """, (paso_actual + 1, json.dumps(contexto_dict), sesion_id))
                                
                                # 4. Si era el último paso, hacer HANDOFF al lead y cerrar sesión
                                if paso_actual == len(pasos) - 1:
                                    # 🎯 HANDOFF: Copiar el contexto recolectado al lead ANTES de limpiar
                                    if contexto_dict:  # Solo si hay datos que guardar
                                        try:
                                            # Obtener el lead_id del remitente
                                            cursor.execute("""
                                                SELECT id, contexto FROM leads 
                                                WHERE telefono = %s AND cliente_id = %s
                                            """, (remitente, cliente_id))
                                            lead_row = cursor.fetchone()
                                            
                                            if lead_row:
                                                lead_id = lead_row[0]
                                                contexto_existente = lead_row[1] if lead_row[1] else {}
                                                
                                                # Si el contexto existente es string, parsearlo
                                                if isinstance(contexto_existente, str):
                                                    try:
                                                        contexto_existente = json.loads(contexto_existente)
                                                    except:
                                                        contexto_existente = {}
                                                
                                                # 🔄 MERGE: Combinar contexto existente con el nuevo
                                                contexto_mergeado = {**contexto_existente, **contexto_dict}
                                                
                                                # Agregar metadata del flujo completado
                                                contexto_mergeado["_ultimo_flujo"] = {
                                                    "nombre": nombre_flujo,
                                                    "completado_en": datetime.now().isoformat(),
                                                    "datos_recolectados": contexto_dict
                                                }
                                                
                                                # Guardar en el lead
                                                cursor.execute("""
                                                    UPDATE leads 
                                                    SET contexto = %s::jsonb
                                                    WHERE id = %s
                                                """, (json.dumps(contexto_mergeado), lead_id))
                                                
                                                print(f"🎯 [HANDOFF] Contexto del flujo '{nombre_flujo}' copiado al lead {remitente}: {list(contexto_dict.keys())}")
                                        except Exception as e:
                                            print(f"⚠️ Error copiando contexto al lead: {e}")
                                    
                                    # Ahora sí, cerrar la sesión del bot
                                    cursor.execute("""
                                        UPDATE conversation_sessions 
                                        SET estado_actual = 'idle', flujo_activo_id = NULL, 
                                            paso_actual = 0, contexto = '{}'::jsonb
                                        WHERE id = %s
                                    """, (sesion_id,))
                        else:
                            # Flujo terminado inesperadamente, reiniciar
                            cursor.execute("""
                                UPDATE conversation_sessions 
                                SET estado_actual = 'idle', flujo_activo_id = NULL, paso_actual = 0, contexto = '{}'::jsonb
                                WHERE id = %s
                            """, (sesion_id,))

                # ==========================================
                # PASO 2: ¿El mensaje DISPARA un nuevo flujo? (Solo si no hay flujo activo)
                # ==========================================
                if not flujo_activo_encontrado:
                    cursor.execute("""
                        SELECT id, pasos FROM bot_flows 
                        WHERE cliente_id = %s AND activo = true 
                          AND unaccent(LOWER(trigger_keyword)) = unaccent(LOWER(%s))
                        LIMIT 1
                    """, (cliente_id, mensaje_limpio))
                    
                    flujo_trigger = cursor.fetchone()
                    if flujo_trigger:
                        flujo_id, pasos_json = flujo_trigger
                        pasos = pasos_json if isinstance(pasos_json, list) else []
                        
                        if pasos:
                            primer_paso = pasos[0]
                            bot_response = primer_paso.get("texto")
                            nivel_match = f"FLUJO INICIADO: {mensaje_limpio}"
                            flujo_activo_encontrado = True
                            
                            cursor.execute("""
                                INSERT INTO conversation_sessions 
                                (cliente_id, external_user_id, platform, estado_actual, flujo_activo_id, paso_actual, contexto, ultimo_input_en)
                                VALUES (%s, %s, 'whatsapp', 'active', %s, 1, '{}'::jsonb, NOW())
                                ON CONFLICT (cliente_id, external_user_id, platform) 
                                DO UPDATE SET 
                                    estado_actual = 'active',
                                    flujo_activo_id = EXCLUDED.flujo_activo_id,
                                    paso_actual = EXCLUDED.paso_actual,
                                    contexto = EXCLUDED.contexto,
                                    ultimo_input_en = NOW()
                            """, (cliente_id, remitente, flujo_id))

                # ==========================================
                # PASO 3: TUS KEYWORDS (Exactamente como funcionaban, protegidas por el 'if')
                # ==========================================
                if not flujo_activo_encontrado:
                    # NIVEL 1: Coincidencia EXACTA
                    cursor.execute("""
                        SELECT id, respuesta 
                        FROM bot_keywords 
                        WHERE cliente_id = %s 
                          AND activo = true
                          AND unaccent(LOWER(keyword)) = unaccent(LOWER(%s))
                        LIMIT 1
                    """, (cliente_id, mensaje_limpio))
                    
                    resultado = cursor.fetchone()
                    
                    if resultado:
                        keyword_id_usada = resultado[0]
                        bot_response = resultado[1]
                        nivel_match = "EXACTO"
                    else:
                        # NIVEL 2: La keyword está CONTENIDA en el mensaje
                        cursor.execute("""
                            SELECT id, respuesta 
                            FROM bot_keywords 
                            WHERE cliente_id = %s 
                              AND activo = true
                              AND unaccent(LOWER(%s)) LIKE CONCAT('%%', unaccent(LOWER(keyword)), '%%')
                            ORDER BY LENGTH(keyword) DESC
                            LIMIT 1
                        """, (cliente_id, mensaje_limpio))
                        
                        resultado = cursor.fetchone()
                        
                        if resultado:
                            keyword_id_usada = resultado[0]
                            bot_response = resultado[1]
                            nivel_match = "CONTENIDO"
                        else:
                            # NIVEL 3: Coincidencia por SIMILITUD (para typos)
                            # ⚠️ NOTA: El orden de parámetros aquí es (mensaje_limpio, cliente_id) 
                            # porque el primer %s es LOWER(%s) y el segundo es cliente_id = %s
                            cursor.execute("""
                                SELECT id, respuesta, similarity(unaccent(LOWER(keyword)), unaccent(LOWER(%s))) as sim
                                FROM bot_keywords 
                                WHERE cliente_id = %s 
                                  AND activo = true
                                ORDER BY sim DESC
                                LIMIT 1
                            """, (mensaje_limpio, cliente_id))
                            
                            resultado = cursor.fetchone()
                            
                            if resultado and resultado[2] > 0.3:
                                keyword_id_usada = resultado[0]
                                bot_response = resultado[1]
                                nivel_match = f"SIMILITUD ({resultado[2]:.0%})"
                    
                    # Si encontramos una keyword, actualizamos sus estadísticas
                    if resultado and keyword_id_usada:
                        print(f"🤖 [AUTO-RESPUESTA - {nivel_match}] Keyword #{keyword_id_usada} para {remitente}: '{bot_response}'")
                        
                        cursor.execute("""
                            UPDATE bot_keywords 
                            SET veces_usada = COALESCE(veces_usada, 0) + 1,
                                ultima_usada_en = NOW()
                            WHERE id = %s
                        """, (keyword_id_usada,))
                else:
                    # Si fue un flujo, también lo registramos en los logs
                    print(f"🤖 [AUTO-RESPUESTA - {nivel_match}] Para {remitente}: '{bot_response}'")

            except Exception as e:
                import sys
                print(f"⚠️ Error buscando respuestas en BD: {e}", flush=True)
                import traceback
                traceback.print_exc(file=sys.stdout)


            # ========================================================================
            # 📤 ENVIAR RESPUESTA AUTOMÁTICA
            # ========================================================================

            if bot_response:
                try:
                    enviar_respuesta_whatsapp_tenant(
                        cursor=cursor,
                        cliente_id=cliente_id,
                        telefono=remitente,
                        respuesta=bot_response
                    )

                    print(
                        f"✅ [BOT] Respuesta automática enviada -> "
                        f"cliente_id={cliente_id}, "
                        f"telefono={remitente}"
                    )

                except Exception as envio_error:
                    print(
                        f"❌ [BOT] Error enviando respuesta automática -> "
                        f"cliente_id={cliente_id}: "
                        f"{envio_error}"
                    )

        # ✅ 3.5. Guardamos los cambios de stats de keyword/flujo
        conn.commit()

        # 4. Devolver la respuesta al Bot
        return jsonify({
            "ok": True,
            "mensaje": "Mensaje recibido y procesado"
        }), 200

    except Exception as e:
        conn.rollback()
        print(f"❌ Error en /recibir_mensaje: {str(e)}")
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        liberar_db(conn)


# ============================================================================
# 2. ENVIAR MENSAJES DESDE EL CRM (CRM -> BOT/WHATSAPP)
# ============================================================================
@app.route("/enviar_mensaje", methods=["POST"])
def enviar_mensaje():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

    datos = request.json
    telefono = datos.get("telefono")
    tipo = datos.get("tipo", "texto")
    mensaje_texto = datos.get("mensaje")
    caption = datos.get("caption", "")

    if not telefono:
        return jsonify({"error": "Número de teléfono es obligatorio"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # 1. Obtener exclusivamente la integración del tenant autenticado.
        cursor.execute("""
            SELECT whatsapp_access_token, whatsapp_phone_number_id, bot_url 
            FROM tenant_integraciones 
            WHERE cliente_id = %s
        """, (cliente_id,))
        config = cursor.fetchone()

        if not config:
            return jsonify({"error": "No existe una integración de WhatsApp configurada para este negocio."}), 400

        token_encriptado, phone_id, bot_url_tenant = config

        if not token_encriptado:
            return jsonify({"error": "Falta configurar el Access Token de WhatsApp para este negocio."}), 400

        if not phone_id or not str(phone_id).strip():
            return jsonify({"error": "Falta configurar el Phone Number ID de WhatsApp para este negocio."}), 400

        token = desencriptar_credencial(token_encriptado)
        if not token or not token.strip():
            return jsonify({"error": "No se pudo obtener un Access Token válido para este negocio."}), 400

        phone_id = str(phone_id).strip()

        # CAMIBOT_API_URL es infraestructura compartida del gateway Node; no es
        # una credencial WhatsApp ni sustituye token/phone_id del tenant.
        bot_url = bot_url_tenant or os.getenv("CAMIBOT_API_URL", "http://localhost:3001")

        secreto_interno = os.getenv("BOT_INTERNAL_SECRET")
        if not secreto_interno:
            app.logger.error("❌ BOT_INTERNAL_SECRET no está configurado en Flask")
            return jsonify({"error": "Configuración interna de mensajería incompleta."}), 500

        headers = {
            "X-Bot-Secret": secreto_interno,
            "Content-Type": "application/json"
        }

        # 2. Preparar payload para Node sin enviar cliente_id.
        payload = {
            "telefono": telefono,
            "whatsapp_token": token,
            "whatsapp_phone_id": phone_id
        }
        
        if tipo == "imagen":
            payload.update({
                "imageUrl": mensaje_texto,
                "caption": caption,
                "tipo": "imagen",
                "reportar_al_crm": False
            })
            endpoint = f"{bot_url.rstrip('/')}/enviar_imagen"
        elif tipo == "video":
            payload.update({
                "videoUrl": mensaje_texto,
                "caption": caption,
                "tipo": "video",
                "reportar_al_crm": False
            })
            endpoint = f"{bot_url.rstrip('/')}/enviar_video"
        else:
            payload.update({
                "mensaje": mensaje_texto,
                "tipo": "texto",
                # Flask persiste y emite el texto manual; Node sólo transporta.
                "reportar_al_crm": False
            })
            endpoint = f"{bot_url.rstrip('/')}/enviar_mensaje"

        # 3. Registrar el intento sin marcarlo como enviado antes de tiempo.
        tipos_persistidos = {
            "imagen": "enviado_imagen",
            "video": "enviado_video"
        }
        tipo_persistido = tipos_persistidos.get(tipo, "enviado")
        cursor.execute("""
            INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo, cliente_id, fecha)
            VALUES ('web', %s, %s, 'Pendiente', %s, %s, NOW())
            RETURNING id, fecha
        """, (telefono, mensaje_texto, tipo_persistido, cliente_id))
        mensaje_id, fecha_mensaje = cursor.fetchone()
        conn.commit()

        exito = False
        # Sin una idempotency key durable, cada intento manual realiza una
        # sola llamada. Flask espera más que el timeout Meta de cada helper.
        timeout_gateway = 25 if tipo == "imagen" else 35 if tipo == "video" else 20
        try:
            r = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout_gateway
            )
            if r.status_code == 200:
                exito = True
            elif r.status_code == 504:
                app.logger.warning(
                    "resultado_ambiguo_timeout: "
                    f"cliente_id={cliente_id}, mensaje_id={mensaje_id}"
                )
            else:
                app.logger.warning(
                    "envio_whatsapp_fallido_gateway: "
                    f"cliente_id={cliente_id}, mensaje_id={mensaje_id}, "
                    f"http_status={r.status_code}"
                )
        except requests.exceptions.Timeout:
            app.logger.warning(
                "resultado_ambiguo_timeout: "
                f"cliente_id={cliente_id}, mensaje_id={mensaje_id}"
            )
        except requests.exceptions.RequestException as e:
            app.logger.warning(
                "resultado_ambiguo_error_red: "
                f"cliente_id={cliente_id}, mensaje_id={mensaje_id}, "
                f"tipo_error={type(e).__name__}"
            )
        
        if not exito:
            cursor.execute("""
                UPDATE mensajes SET estado = 'Fallido' 
                WHERE id = %s AND cliente_id = %s
            """, (mensaje_id, cliente_id))
            conn.commit()
            return jsonify({"error": "No se pudo conectar con el servicio de mensajería"}), 500

        cursor.execute("""
            UPDATE mensajes SET estado = 'Enviado'
            WHERE id = %s AND cliente_id = %s
        """, (mensaje_id, cliente_id))
        conn.commit()

        # Flask publica el envío manual sólo después de confirmar a Node y
        # persistir estado='Enviado', siempre en la room del tenant.
        try:
            socketio.emit(
                "nuevo_mensaje",
                {
                    "id": mensaje_id,
                    "remitente": telefono,
                    "mensaje": mensaje_texto,
                    "tipo": tipo_persistido,
                    "estado": "Enviado",
                    "fecha": fecha_mensaje.isoformat(),
                    "cliente_id": cliente_id,
                    "whatsapp_media_id": None,
                    "media_url": None
                },
                room=f"cliente_{cliente_id}"
            )
        except Exception as emit_error:
            app.logger.warning(
                "No se pudo emitir el mensaje saliente por Socket.IO: "
                f"cliente_id={cliente_id}, mensaje_id={mensaje_id}, "
                f"error={emit_error}"
            )

        return jsonify({"mensaje": "Mensaje enviado correctamente"}), 200

    except Exception as e:
        conn.rollback()
        print(f"❌ Error CRÍTICO en /enviar_mensaje: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        liberar_db(conn)


# ============================================================================
# 3. SUBIDA DE IMÁGENES DESDE EL CRM (Multi-tenant Dinámico)
# ============================================================================
@app.route("/api/chat/upload_imagen", methods=["POST"])
def upload_imagen_chat():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 404

    if 'imagen' not in request.files:
        return jsonify({"error": "No se encontró el archivo"}), 400
    
    file = request.files['imagen']
    if file.filename == '':
        return jsonify({"error": "Nombre de archivo vacío"}), 400

    try:
        # Crear carpeta si no existe
        uploads_dir = os.path.join('static', 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        
        # Generar nombre único con cliente_id
        ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
        filename = f"cliente_{cliente_id}_{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(uploads_dir, filename)
        
        file.save(filepath)
        
        # ✅ SOLUCIÓN MULTI-TENANT: Obtener el dominio dinámicamente
        # request.host_url devuelve algo como "https://camicam.eventa.com.mx/"
        dominio_base = request.host_url.rstrip('/')
        url_publica = f"{dominio_base}/static/uploads/{filename}"
        
        return jsonify({"url": url_publica}), 200
        
    except Exception as e:
        print(f"❌ Error en upload_imagen_chat: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error al subir imagen"}), 500

    
# ============================================================================
# 4. OBTENER MENSAJES (LISTA DE CHATS Y DETALLE)
# ============================================================================
@app.route("/api/media/<int:media_id>", methods=["GET"])
def obtener_media_privada(media_id):
    if not g.current_user:
        return jsonify({"error": "No autorizado"}), 401

    cliente_id = g.current_user["cliente_id"]
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                s3_bucket,
                s3_key,
                mime_type,
                original_filename
            FROM whatsapp_media
            WHERE id = %s
              AND cliente_id = %s
        """, (media_id, cliente_id))
        media = cursor.fetchone()

        if not media:
            return jsonify({"error": "Media no encontrada"}), 404

        bucket, key, _mime_type, _original_filename = media
        cliente_s3, _bucket_configurado = _crear_cliente_s3_media()
        expiracion = _obtener_entero_positivo_env(
            "WHATSAPP_MEDIA_PRESIGNED_EXPIRES_SECONDS",
            300
        )
        url_firmada = cliente_s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key
            },
            ExpiresIn=expiracion
        )
        return redirect(url_firmada, code=302)
    except ErrorProcesamientoMedia:
        app.logger.error(
            "Configuración incompleta al generar acceso a media privada: "
            f"cliente_id={cliente_id}, media_id={media_id}"
        )
        return jsonify({"error": "No se pudo obtener la media"}), 500
    except Exception as e:
        app.logger.error(
            "Error al generar acceso a media privada: "
            f"cliente_id={cliente_id}, media_id={media_id}, "
            f"tipo_error={type(e).__name__}"
        )
        return jsonify({"error": "No se pudo obtener la media"}), 500
    finally:
        liberar_db(conn)


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
        # Obtenemos los últimos mensajes para armar la lista de chats
        cursor.execute("""
            SELECT DISTINCT ON (remitente)
                remitente,
                mensaje,
                tipo,
                fecha,
                whatsapp_media_id
            FROM mensajes 
            WHERE cliente_id = %s 
            ORDER BY remitente, fecha DESC
        """, (cliente_id,))
        mensajes = [dict(row) for row in cursor.fetchall()]
        for mensaje in mensajes:
            media_id = mensaje.get("whatsapp_media_id")
            mensaje["media_url"] = (
                f"/api/media/{media_id}" if media_id is not None else None
            )
        return jsonify(mensajes)
    except Exception as e:
        print("❌ Error en /mensajes:", str(e))
        return jsonify([])
    finally:
        liberar_db(conn)


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
        
        # Obtener nombre del lead
        cursor.execute("SELECT nombre FROM leads WHERE telefono = %s AND cliente_id = %s", (remitente, cliente_id))
        lead = cursor.fetchone()
        nombre_lead = lead["nombre"] if lead else remitente

        # Obtener historial de mensajes ordenado cronológicamente
        cursor.execute("""
            SELECT id, mensaje, tipo, fecha, whatsapp_media_id
            FROM mensajes 
            WHERE remitente = %s AND cliente_id = %s 
            ORDER BY fecha ASC
        """, (remitente, cliente_id))
        mensajes = [dict(row) for row in cursor.fetchall()]
        for mensaje in mensajes:
            media_id = mensaje.get("whatsapp_media_id")
            mensaje["media_url"] = (
                f"/api/media/{media_id}" if media_id is not None else None
            )

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
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404

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
            INSERT INTO anio_color (anio, color, cliente_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (anio, cliente_id)
            DO UPDATE SET color = EXCLUDED.color
        """, (anio, color, cliente_id))
        conn.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en agregar_anio_color: {str(e)}")
        import traceback
        traceback.print_exc()
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
    servicios_input = data.get("servicios", {})
    metadatos_input = data.get("metadatos", {})

    if not fecha_str:
        return jsonify({"error": "Falta la fecha en formato YYYY-MM-DD"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        ticket_value = float(ticket) if ticket else 0.0

        # Validar y preparar servicios
        if isinstance(servicios_input, str):
            try:
                servicios_json = json.loads(servicios_input)
            except:
                servicios_json = {}
        elif isinstance(servicios_input, dict):
            servicios_json = servicios_input
        else:
            servicios_json = {}

        # Validar y preparar metadatos
        if isinstance(metadatos_input, str):
            try:
                metadatos_json = json.loads(metadatos_input)
            except:
                metadatos_json = {}
        elif isinstance(metadatos_input, dict):
            metadatos_json = metadatos_input
        else:
            metadatos_json = {}

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
            INSERT INTO calendario (fecha, lead_id, titulo, notas, ticket, servicios, metadatos, cliente_id)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        """, (
            fecha_local.date(),
            lead_id,
            titulo,
            notas,
            ticket_value,
            json.dumps(servicios_json),
            json.dumps(metadatos_json),
            cliente_id
        ))
        conn.commit()

        socketio.emit(
            "calendario_actualizado",
            {"accion": "nueva_fecha", "anio": fecha_local.year, "fecha": fecha_str, "titulo": titulo},
            room=f"cliente_{cliente_id}"
        )

        return jsonify({
            "ok": True,
            "mensaje": f"Fecha {fecha_str} agregada correctamente."
        }), 200

    except Exception as e:
        print(f"❌ Error en /calendario/agregar_manual: {str(e)}")
        import traceback
        traceback.print_exc()
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
            SELECT id, fecha, lead_id, titulo, notas, ticket, servicios, metadatos
            FROM calendario
            WHERE id = %s AND cliente_id = %s
        """, (cal_id, cliente_id))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Registro no encontrado"}), 404

        respuesta = {
            "id": row[0],
            "fecha": str(row[1]),
            "lead_id": row[2],
            "titulo": row[3] or "",
            "notas": row[4] or "",
            "ticket": float(row[5]) if row[5] else 0.0,
            "servicios": row[6] if row[6] else {},
            "metadatos": row[7] if row[7] else {}
        }
        return jsonify(respuesta), 200
    except Exception as e:
        print(f"❌ Error en /calendario/detalle: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)


# Editar_calendario
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
    metadatos_input = data.get("metadatos", {})
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500
    try:
        cursor = conn.cursor()
        ticket_value = float(ticket) if ticket else 0.0
        
        # Validar servicios
        if not isinstance(servicios_input, dict):
            servicios_input = {}
        # Validar metadatos
        if not isinstance(metadatos_input, dict):
            metadatos_input = {}
        
        # ✅ OBTENER LA FECHA ANTES DE ACTUALIZAR (para el evento socket)
        cursor.execute("SELECT fecha FROM calendario WHERE id = %s AND cliente_id = %s", (cal_id, cliente_id))
        row_fecha = cursor.fetchone()
        fecha_evento = row_fecha[0] if row_fecha else None
        
        cursor.execute("""
        UPDATE calendario
        SET titulo = %s, notas = %s, ticket = %s, servicios = %s::jsonb, metadatos = %s::jsonb
        WHERE id = %s AND cliente_id = %s
        """, (
            titulo,
            notas,
            ticket_value,
            json.dumps(servicios_input),
            json.dumps(metadatos_input),
            cal_id,
            cliente_id
        ))
        conn.commit()
        
        if cursor.rowcount == 0:
            return jsonify({"error": "No se encontró esa fecha"}), 404
        
        # ✅ EMITIR EVENTO SOCKET PARA ACTUALIZACIÓN EN TIEMPO REAL
        if fecha_evento:
            socketio.emit(
                "calendario_actualizado",
                {
                    "accion": "editar_fecha",
                    "anio": fecha_evento.year if fecha_evento else datetime.now().year,
                    "fecha": fecha_evento.strftime("%Y-%m-%d") if fecha_evento else None,
                    "titulo": titulo,
                    "cal_id": cal_id
                },
                room=f"cliente_{cliente_id}"
            )
        
        return jsonify({"ok": True, "mensaje": "Fecha actualizada"}), 200
    except Exception as e:
        print(f"❌ Error en /calendario/editar: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)
        
# Eliminar_calendario
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
        
        # ✅ OBTENER LA FECHA ANTES DE ELIMINAR (para el evento socket)
        cursor.execute("SELECT fecha FROM calendario WHERE id = %s AND cliente_id = %s", (cal_id, cliente_id))
        row_fecha = cursor.fetchone()
        fecha_evento = row_fecha[0] if row_fecha else None
        
        cursor.execute("DELETE FROM calendario WHERE id = %s AND cliente_id = %s", (cal_id, cliente_id))
        conn.commit()
        
        if cursor.rowcount == 0:
            return jsonify({"error": "No se encontró ese ID"}), 404
        
        # ✅ EMITIR EVENTO SOCKET PARA ACTUALIZACIÓN EN TIEMPO REAL
        if fecha_evento:
            socketio.emit(
                "calendario_actualizado",
                {
                    "accion": "eliminar_fecha",
                    "anio": fecha_evento.year if fecha_evento else datetime.now().year,
                    "fecha": fecha_evento.strftime("%Y-%m-%d") if fecha_evento else None,
                    "cal_id": cal_id
                },
                room=f"cliente_{cliente_id}"
            )
        
        return jsonify({"ok": True, "mensaje": "Fecha eliminada"}), 200
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
            VALUES (%s, %s, %s)
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
    
    # ✅ OBTENER cliente_id DEL SUBDOMINIO
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404
    
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
          AND cliente_id = %s              
        """, (mes, anio, cliente_id))      
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
    
    # ✅ OBTENER cliente_id DEL SUBDOMINIO
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500
    try:
        cursor = conn.cursor()
        
        # ── 1) Ingresos por mes (CON FILTRO cliente_id) ─────────────────────────
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM fecha)::int   AS mes,
                   COALESCE(SUM(ticket),0)          AS total_ingresos
            FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
              AND cliente_id = %s              -- ✅ FILTRO AGREGADO
            GROUP BY mes
        """, (anio, cliente_id))  # ✅ PASAR cliente_id COMO PARÁMETRO
        
        ingresos_por_mes = {m:0.0 for m in range(1,13)}
        for mes, total in cursor.fetchall():
            ingresos_por_mes[int(mes)] = float(total)
        
        # ── 2) Gastos reales por mes (CON FILTRO cliente_id) ────────────────────
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM fecha)::int   AS mes,
                   COALESCE(SUM(monto),0)           AS total_gastos
            FROM gastos
            WHERE EXTRACT(YEAR FROM fecha) = %s
              AND cliente_id = %s              -- ✅ FILTRO AGREGADO
            GROUP BY mes
        """, (anio, cliente_id))
        
        gastos_por_mes = {m:0.0 for m in range(1,13)}
        for mes, total in cursor.fetchall():
            gastos_por_mes[int(mes)] = float(total)
        
        # ── 3) Costos finales = max(gastos, 30 % ingresos) ──
        costos_por_mes = {}
        for m in range(1,13):
            min_30 = ingresos_por_mes[m] * 0.30
            costos_por_mes[m] = max(gastos_por_mes[m], min_30)
        
        # ── 4) Número total de eventos del año (CON FILTRO cliente_id) ──────────
        cursor.execute("""
            SELECT COUNT(*) FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
              AND cliente_id = %s              -- ✅ FILTRO AGREGADO
        """, (anio, cliente_id))
        total_eventos = cursor.fetchone()[0] or 0
        
        return jsonify({
            "anio": int(anio),
            "ingresos_anual": ingresos_por_mes,
            "costos_anual":   costos_por_mes,
            "total_eventos":  total_eventos
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        liberar_db(conn)



# 📌 Endpoint para reporte de servicios contratados (DINÁMICO por tenant)
@app.route("/reportes/servicios_anual", methods=["GET"])
def reporte_servicios_anual():
    anio = request.args.get("anio")
    if not anio:
        return jsonify({"error": "Falta año"}), 400
    
    # ✅ OBTENER cliente_id DEL SUBDOMINIO
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "Cliente no autorizado"}), 404
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar DB"}), 500
    
    try:
        cursor = conn.cursor()
        
        # 🔹 1. Obtener servicios configurados por este tenant (CON FILTRO)
        cursor.execute("""
            SELECT clave, nombre, tipo
            FROM servicios_tenant
            WHERE cliente_id = %s AND activo = true
            ORDER BY nombre
        """, (cliente_id,))
        servicios_config = cursor.fetchall()
        
        if not servicios_config:
            return jsonify({
                "anio": int(anio),
                "servicios": [],
                "mensaje": "No hay servicios configurados para este tenant"
            }), 200
        
        # 🔹 2. Construir consulta dinámica para contar cada servicio
        select_parts = []
        for clave, nombre, tipo in servicios_config:
            if tipo == 'number':
                # Para servicios numéricos: SUMAR las cantidades
                select_parts.append(f"""
                    COALESCE(SUM((servicios->>'{clave}')::int), 0) AS "{clave}"
                """)
            else:
                # Para servicios boolean: CONTAR cuántas veces se marcaron
                select_parts.append(f"""
                    COALESCE(SUM(CASE WHEN (servicios->>'{clave}')::int = 1 THEN 1 ELSE 0 END), 0) AS "{clave}"
                """)
        
        # 🔹 3. Query principal CON FILTRO cliente_id
        query = f"""
            SELECT {', '.join(select_parts)}
            FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s 
              AND cliente_id = %s              -- ✅ FILTRO AGREGADO
        """
        
        cursor.execute(query, (anio, cliente_id))  # ✅ PASAR cliente_id
        row = cursor.fetchone()
        
        # 🔹 4. Construir respuesta con nombres legibles
        servicios_reporte = []
        for i, (clave, nombre, tipo) in enumerate(servicios_config):
            cantidad = row[i] if row and row[i] is not None else 0
            servicios_reporte.append({
                "clave": clave,
                "nombre": nombre,
                "tipo": tipo,
                "cantidad": int(cantidad)
            })
        
        # 🔹 5. Ordenar: primero boolean (checkbox), luego number (cantidad)
        servicios_reporte.sort(key=lambda s: (0 if s['tipo'] == 'boolean' else 1, s['nombre']))
        
        return jsonify({
            "anio": int(anio),
            "servicios": servicios_reporte,
            "total_servicios": len(servicios_reporte)
        }), 200
        
    except Exception as e:
        print(f"❌ Error en /reportes/servicios_anual: {str(e)}")
        import traceback
        traceback.print_exc()
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
    print(f"🔍 DEBUG agregar_etiqueta - Iniciando solicitud")
    
    cliente_id = obtener_cliente_id_de_subdominio()
    print(f"🔍 DEBUG agregar_etiqueta - cliente_id: {cliente_id}")
    
    if not cliente_id:
        print("❌ ERROR agregar_etiqueta - Sin cliente_id")
        return jsonify({"error": "Cliente no autorizado"}), 404

    data = request.json
    print(f"🔍 DEBUG agregar_etiqueta - Datos: {data}")
    
    etiqueta = data.get("etiqueta")
    if not etiqueta:
        print("❌ ERROR agregar_etiqueta - Sin etiqueta")
        return jsonify({"error": "Falta el nombre de la etiqueta"}), 400

    conn = conectar_db()
    if not conn:
        print("❌ ERROR agregar_etiqueta - Sin conexión BD")
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        print(f"🔍 DEBUG agregar_etiqueta - Ejecutando INSERT con cliente_id={cliente_id}, etiqueta={etiqueta}")
        
        cursor.execute("""
            INSERT INTO gasto_etiquetas (etiqueta, cliente_id)
            VALUES (%s, %s)
            ON CONFLICT (etiqueta, cliente_id) DO NOTHING
        """, (etiqueta, cliente_id))
        
        conn.commit()
        print("✅ DEBUG agregar_etiqueta - Éxito")
        return jsonify({"ok": True, "mensaje": "Etiqueta creada correctamente."}), 200
        
    except Exception as e:
        print(f"💥 ERROR CRÍTICO agregar_etiqueta: {str(e)}")
        import traceback
        traceback.print_exc()
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
        

#''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''
#--------------SECION DE CONFIGURACION SUPER-USUARIO PANEL DE ADMINISTRACION-----------------
#,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,


# Middleware de autenticación
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Verificar si el usuario está autenticado y es superadmin
        if 'user_id' not in session:
            return redirect(url_for('admin_login'))  # ✅ Cambiado a admin_login
        
        user_id = session['user_id']
        conn = conectar_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT r.name 
            FROM user_roles ur
            JOIN roles r ON ur.role_id = r.id
            WHERE ur.user_id = %s AND r.name = 'superadmin'
        """, (user_id,))
        
        is_admin = cur.fetchone() is not None
        liberar_db(conn)
        
        if not is_admin:
            return redirect(url_for('admin_login'))  # ✅ Cambiado a admin_login
            
        return f(*args, **kwargs)
    return decorated_function


# Iniciar sesion
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Login exclusivo para administradores del sistema"""
    if request.method == "GET":
        return render_template("admin/login.html")
    
    try:
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
            # ✅ Buscar usuario SIN verificar cliente_id (es admin global)
            cur.execute("""
                SELECT id, password_hash 
                FROM users 
                WHERE email = %s AND activo = true
            """, (email,))
            
            user = cur.fetchone()
            
            if not user or not check_password_hash(user[1], password):
                return jsonify({"error": "Credenciales inválidas"}), 401

            # Verificar que sea superadmin
            cur.execute("""
                SELECT r.name 
                FROM user_roles ur
                JOIN roles r ON ur.role_id = r.id
                WHERE ur.user_id = %s AND r.name = 'superadmin'
            """, (user[0],))
            
            if not cur.fetchone():
                return jsonify({"error": "Acceso denegado. Se requiere rol de superadministrador."}), 403

            # Iniciar sesión
            session['user_id'] = user[0]
            session['is_admin'] = True  # Marcar como admin global
            
            return jsonify({"mensaje": "Login exitoso"}), 200
            
        except Exception as e:
            print(f"❌ Error en admin login: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({"error": "Error interno"}), 500
        finally:
            liberar_db(conn)
            
    except Exception as e:
        print(f"💥 ERROR CRÍTICO EN ADMIN LOGIN: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno"}), 500
    
    
# Cerrar sesion
@app.route("/admin/logout")
def admin_logout():
    """Cerrar sesión de administrador"""
    session.pop('user_id', None)
    session.pop('is_admin', None)
    session.pop('cliente_id', None)
    return redirect(url_for('admin_login'))


#Rutas de administración
@app.route("/admin")
@admin_required
def admin_dashboard():
    """Dashboard principal de administración"""
    conn = conectar_db()
    cur = conn.cursor()
    
    # Estadísticas generales
    cur.execute("SELECT COUNT(*) FROM clientes")
    total_tenants = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM clientes WHERE activo = true")
    active_tenants = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM clientes WHERE email_verificado = true")
    verified_tenants = cur.fetchone()[0]
    
    # Últimos 10 tenants
    cur.execute("""
        SELECT 
            id, nombre, subdominio, email_admin, plan, 
            activo, email_verificado, creado_en
        FROM clientes 
        ORDER BY creado_en DESC
        LIMIT 10
    """)
    recent_tenants = cur.fetchall()
    
    liberar_db(conn)
    
    return render_template("admin/dashboard.html", 
                         total_tenants=total_tenants,
                         active_tenants=active_tenants,
                         verified_tenants=verified_tenants,
                         recent_tenants=recent_tenants)

@app.route("/admin/tenants")
@admin_required
def admin_tenants():
    """Lista completa de tenants"""
    conn = conectar_db()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT 
            id, nombre, subdominio, email_admin, plan, 
            activo, email_verificado, creado_en
        FROM clientes 
        ORDER BY creado_en DESC
    """)
    
    tenants = cur.fetchall()
    liberar_db(conn)
    
    return render_template("admin/tenants.html", tenants=tenants)

@app.route("/admin/tenant/<int:tenant_id>/disable")
@admin_required
def admin_disable_tenant(tenant_id):
    """Desactivar tenant"""
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("UPDATE clientes SET activo = false WHERE id = %s", (tenant_id,))
    conn.commit()
    liberar_db(conn)
    return redirect(url_for('admin_tenants'))

@app.route("/admin/tenant/<int:tenant_id>/enable")
@admin_required
def admin_enable_tenant(tenant_id):
    """Activar tenant"""
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("UPDATE clientes SET activo = true WHERE id = %s", (tenant_id,))
    conn.commit()
    liberar_db(conn)
    return redirect(url_for('admin_tenants'))

@app.route("/admin/tenant/<int:tenant_id>/delete")
@admin_required
def admin_delete_tenant(tenant_id):
    """Eliminar tenant (con confirmación en frontend)"""
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM clientes WHERE id = %s", (tenant_id,))
    conn.commit()
    liberar_db(conn)
    return redirect(url_for('admin_tenants'))

@app.route("/admin/tenant/<int:tenant_id>/upgrade")
@admin_required
def admin_upgrade_tenant(tenant_id):
    """Actualizar a plan premium"""
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("UPDATE clientes SET plan = 'premium' WHERE id = %s", (tenant_id,))
    conn.commit()
    liberar_db(conn)
    return redirect(url_for('admin_tenants'))

@app.route("/admin/tenant/<int:tenant_id>/downgrade")
@admin_required
def admin_downgrade_tenant(tenant_id):
    """Actualizar a plan básico"""
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("UPDATE clientes SET plan = 'basico' WHERE id = %s", (tenant_id,))
    conn.commit()
    liberar_db(conn)
    return redirect(url_for('admin_tenants'))

#Ruta para el boton de CREAR NUEVO TENANT
@app.route("/admin/crear_tenant", methods=["POST"])
@admin_required
def admin_crear_tenant():
    """Crea un nuevo tenant directamente desde el panel de administración"""
    try:
        datos = request.json
        nombre = datos.get("nombre", "").strip()
        subdominio = datos.get("subdominio", "").strip().lower()
        email = datos.get("email", "").strip().lower()
        plan = datos.get("plan", "basico")
        
        # Validaciones básicas
        if not nombre or not subdominio or not email:
            return jsonify({"error": "Todos los campos son requeridos"}), 400
        
        # Usamos tu función existente de validación
        if not validar_subdominio(subdominio):
            return jsonify({"error": "Subdominio inválido o reservado"}), 400
            
        if plan not in ['basico', 'premium']:
            return jsonify({"error": "Plan inválido"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión a la base de datos"}), 500

        try:
            cur = conn.cursor()
            
            # 1. Verificar si el subdominio ya existe
            cur.execute("SELECT id FROM clientes WHERE subdominio = %s", (subdominio,))
            if cur.fetchone():
                return jsonify({"error": "Este subdominio ya está en uso"}), 400
                
            # 2. Verificar si el email ya está registrado
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return jsonify({"error": "Este email ya está registrado"}), 400

            # Contraseña por defecto para tenants creados por el admin
            default_password = "#Mishi2023"
            password_hash = generate_password_hash(default_password, method='pbkdf2:sha256', salt_length=8)

            # 3. Crear el cliente (tenant)
            # Como lo crea un admin, lo marcamos como email_verificado = true automáticamente
            query_cliente = """
                INSERT INTO clientes (
                    nombre, subdominio, plan, activo, 
                    email_verificado, email_admin
                )
                VALUES (%(nombre)s, %(subdominio)s, %(plan)s, %(activo)s, 
                        %(email_verificado)s, %(email_admin)s)
                RETURNING id
            """
            params_cliente = {
                'nombre': nombre,
                'subdominio': subdominio,
                'plan': plan,
                'activo': True,
                'email_verificado': True, # ✅ Verificado automáticamente
                'email_admin': email
            }
            cur.execute(query_cliente, params_cliente)
            cliente_id = cur.fetchone()[0]

            # 4. Crear el usuario administrador para este nuevo tenant
            query_usuario = """
                INSERT INTO users (email, password_hash, cliente_id, activo)
                VALUES (%(email)s, %(password_hash)s, %(cliente_id)s, %(activo)s)
                RETURNING id
            """
            params_usuario = {
                'email': email,
                'password_hash': password_hash,
                'cliente_id': cliente_id,
                'activo': True
            }
            cur.execute(query_usuario, params_usuario)
            user_id = cur.fetchone()[0]

            # 5. Asignar rol 'admin' al nuevo usuario
            cur.execute("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT %(user_id)s, id FROM roles WHERE name = 'admin'
            """, {'user_id': user_id})
            
            conn.commit()
            
            return jsonify({
                "mensaje": f"Tenant '{nombre}' creado exitosamente. Contraseña inicial: {default_password}",
                "subdominio": subdominio
            }), 200
            
        except Exception as e:
            conn.rollback()
            print(f"❌ Error en admin_crear_tenant: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({"error": "Error al crear el cliente en la base de datos"}), 500
        finally:
            liberar_db(conn)
            
    except Exception as e:
        print(f"💥 ERROR CRÍTICO EN CREAR TENANT: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno del servidor"}), 500

 
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

 # RUTA PARA SUBIR LOGO 
@app.route("/config/logo", methods=["POST"])
@requires_permission("manage_config")
def subir_logo():
    print(f"🔍 DEBUG - Sesión actual: {session}")
    print(f"🔍 DEBUG - cliente_id en sesión: {session.get('cliente_id')}")
    
    if 'cliente_id' not in session:
        print("❌ ERROR: No hay cliente_id en la sesión")
        return jsonify({"error": "No autorizado"}), 401
    
    cliente_id = session['cliente_id']
    
    file = request.files.get("logo")
    if not file or file.filename == "":
        return jsonify({"error": "Archivo inválido"}), 400

    # Validar tipo de archivo
    if not file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
        return jsonify({"error": "Formato de imagen no soportado"}), 400

    # Convertir a data-URI (base64)
    mime = file.content_type or 'image/png'
    data = base64.b64encode(file.read()).decode()  
    uri = f"data:{mime};base64,{data}"

    # ✅ CORREGIDO: Guardar con cliente_id
    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB no disponible")
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO config (cliente_id, clave, valor)
            VALUES (%s, 'logo_base64', %s)
            ON CONFLICT (cliente_id, clave) 
            DO UPDATE SET valor = EXCLUDED.valor
        """, (cliente_id, uri))
        conn.commit()
    finally:
        liberar_db(conn)

    return jsonify({"url": uri}), 200


# RUTA PARA OBTENER LOGO
@app.route("/config/logo", methods=["GET"])
def obtener_logo():
    if 'cliente_id' not in session:
        return jsonify({"url": "/static/logo/default.png"}), 200
    
    cliente_id = session['cliente_id']
    
    try:
        conn = conectar_db()
        if not conn:
            raise RuntimeError("DB no disponible")
        cur = conn.cursor()
        cur.execute("""
            SELECT valor FROM config 
            WHERE cliente_id = %s AND clave = 'logo_base64'
        """, (cliente_id,))
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



# ============================================================================
# ENDPOINTS: GESTIÓN DE CREDENCIALES POR TENANT
# ============================================================================

@app.route("/api/integraciones", methods=["GET"])
def obtener_integraciones():
    """
    Obtiene credenciales del tenant actual (enmascaradas para seguridad).
    Solo retorna valores reales para campos NO sensibles (phone_number_id, URLs).
    """
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a base de datos"}), 500
    
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                whatsapp_access_token,
                whatsapp_phone_number_id,
                whatsapp_verify_token,
                facebook_page_token,
                instagram_access_token,
                openai_api_key,
                n8n_url,
                n8n_api_key,
                creado_en,
                actualizado_en
            FROM tenant_integraciones
            WHERE cliente_id = %s
        """, (cliente_id,))
        
        row = cur.fetchone()
        
        if row:
            # ✅ Retornar valores enmascarados para campos sensibles
            return jsonify({
                "existe": True,
                "whatsapp_access_token": "••••••••••" if row[0] else "",
                "whatsapp_phone_number_id": row[1] or "",
                "whatsapp_verify_token": "••••••••••" if row[2] else "",
                "facebook_page_token": "••••••••••" if row[3] else "",
                "instagram_access_token": "••••••••••" if row[4] else "",
                "openai_api_key": "••••••••••" if row[5] else "",
                "n8n_url": row[6] or "",
                "n8n_api_key": "••••••••••" if row[7] else "",
                "creado_en": row[8].isoformat() if row[8] else None,
                "actualizado_en": row[9].isoformat() if row[9] else None
            }), 200
        else:
            # No hay registro aún para este tenant
            return jsonify({
                "existe": False,
                "whatsapp_access_token": "",
                "whatsapp_phone_number_id": "",
                "whatsapp_verify_token": "",
                "facebook_page_token": "",
                "instagram_access_token": "",
                "openai_api_key": "",
                "n8n_url": "",
                "n8n_api_key": ""
            }), 200
            
    except Exception as e:
        app.logger.error(f"❌ Error en obtener_integraciones: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        liberar_db(conn)


@app.route("/api/integraciones", methods=["POST"])
def guardar_integraciones():
    """
    Guarda/actualiza credenciales del tenant actual (encriptadas en BD).
    Solo encripta campos sensibles; phone_number_id y URLs se guardan en texto plano.
    """
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    datos = request.json
    if not datos:
        return jsonify({"error": "No se recibieron datos"}), 400

    phone_id_presente = "whatsapp_phone_number_id" in datos
    phone_id_raw = datos.get("whatsapp_phone_number_id")
    if phone_id_presente and phone_id_raw is not None and not isinstance(phone_id_raw, str):
        return jsonify({"error": "WhatsApp Phone Number ID inválido"}), 400

    phone_id_normalizado = None
    if phone_id_presente and phone_id_raw is not None:
        phone_id_normalizado = phone_id_raw.strip() or None

    token_raw = datos.get("whatsapp_access_token")
    if token_raw is not None and not isinstance(token_raw, str):
        return jsonify({"error": "WhatsApp Access Token inválido"}), 400

    token_nuevo = token_raw.strip() if isinstance(token_raw, str) else ""

    # ✅ Encriptar solo campos sensibles antes de guardar
    credenciales = {
        # Sensibles → encriptar
        "whatsapp_access_token": encriptar_credencial(token_nuevo),
        "whatsapp_verify_token": encriptar_credencial(datos.get("whatsapp_verify_token")),
        "facebook_page_token": encriptar_credencial(datos.get("facebook_page_token")),
        "instagram_access_token": encriptar_credencial(datos.get("instagram_access_token")),
        "openai_api_key": encriptar_credencial(datos.get("openai_api_key")),
        "n8n_api_key": encriptar_credencial(datos.get("n8n_api_key")),
        # El Phone ID se normaliza a texto sin espacios o NULL.
        "whatsapp_phone_number_id": phone_id_normalizado,
        "n8n_url": datos.get("n8n_url", "").strip(),
    }
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a base de datos"}), 500
    
    try:
        cur = conn.cursor()

        # La UI envía secretos vacíos para indicar "conservar" y siempre
        # reenvía el Phone ID visible. Para clientes API, omitir Phone ID
        # conserva el actual; enviarlo vacío representa "sin configurar".
        cur.execute("""
            SELECT whatsapp_access_token, whatsapp_phone_number_id
            FROM tenant_integraciones
            WHERE cliente_id = %s
        """, (cliente_id,))
        integracion_actual = cur.fetchone()

        token_actual = integracion_actual[0] if integracion_actual else None
        phone_id_actual = integracion_actual[1] if integracion_actual else None
        phone_id_actual = (
            str(phone_id_actual).strip() or None
            if phone_id_actual is not None
            else None
        )

        phone_id_efectivo = (
            phone_id_normalizado
            if phone_id_presente
            else phone_id_actual
        )

        if (
            phone_id_presente
            and phone_id_normalizado is not None
            and phone_id_normalizado != phone_id_actual
            and not token_nuevo
        ):
            conn.rollback()
            return jsonify({
                "error": "Para cambiar el WhatsApp Phone Number ID debes ingresar también un nuevo Access Token."
            }), 400

        if token_nuevo and phone_id_efectivo is None:
            conn.rollback()
            return jsonify({
                "error": "Para guardar un nuevo Access Token debes configurar también el WhatsApp Phone Number ID."
            }), 400

        if phone_id_efectivo is not None and not (token_nuevo or token_actual):
            conn.rollback()
            return jsonify({
                "error": "Para configurar el WhatsApp Phone Number ID debes ingresar también un Access Token."
            }), 400

        if phone_id_efectivo is not None:
            cur.execute("""
                SELECT cliente_id
                FROM tenant_integraciones
                WHERE whatsapp_phone_number_id = %s
                  AND cliente_id <> %s
            """, (phone_id_efectivo, cliente_id))

            if cur.fetchone():
                conn.rollback()
                return jsonify({
                    "error": "Este WhatsApp Phone Number ID ya está asociado a otro negocio."
                }), 409

        # ✅ INSERT o UPDATE según exista o no el registro para este tenant
        cur.execute("""
            INSERT INTO tenant_integraciones 
            (cliente_id, 
             whatsapp_access_token, whatsapp_phone_number_id, whatsapp_verify_token,
             facebook_page_token, instagram_access_token,
             openai_api_key, n8n_url, n8n_api_key,
             actualizado_en)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (cliente_id) 
            DO UPDATE SET 
                whatsapp_access_token = COALESCE(EXCLUDED.whatsapp_access_token, tenant_integraciones.whatsapp_access_token),
                whatsapp_phone_number_id = EXCLUDED.whatsapp_phone_number_id,
                whatsapp_verify_token = COALESCE(EXCLUDED.whatsapp_verify_token, tenant_integraciones.whatsapp_verify_token),
                facebook_page_token = COALESCE(EXCLUDED.facebook_page_token, tenant_integraciones.facebook_page_token),
                instagram_access_token = COALESCE(EXCLUDED.instagram_access_token, tenant_integraciones.instagram_access_token),
                openai_api_key = COALESCE(EXCLUDED.openai_api_key, tenant_integraciones.openai_api_key),
                n8n_url = EXCLUDED.n8n_url,
                n8n_api_key = COALESCE(EXCLUDED.n8n_api_key, tenant_integraciones.n8n_api_key),
                actualizado_en = CURRENT_TIMESTAMP
        """, (
            cliente_id,
            credenciales["whatsapp_access_token"],
            phone_id_efectivo,
            credenciales["whatsapp_verify_token"],
            credenciales["facebook_page_token"],
            credenciales["instagram_access_token"],
            credenciales["openai_api_key"],
            credenciales["n8n_url"],
            credenciales["n8n_api_key"]
        ))
        
        conn.commit()
        app.logger.info(f"✅ Credenciales actualizadas para cliente_id={cliente_id}")
        return jsonify({"ok": True, "mensaje": "Credenciales actualizadas correctamente"}), 200

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({
            "error": "Este WhatsApp Phone Number ID ya está asociado a otro negocio."
        }), 409

    except Exception as e:
        conn.rollback()
        app.logger.error(f"❌ Error en guardar_integraciones: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Error al guardar credenciales: {str(e)}"}), 500
    finally:
        liberar_db(conn)
   
# ============================================================================
# ENDPOINT: PROBAR CONEXIÓN WHATSAPP BUSINESS API
# ============================================================================
@app.route("/api/integraciones/test-whatsapp", methods=["POST"])
def test_whatsapp_connection():
    """
    Prueba de conexión básica con WhatsApp Business API de Meta.
    Verifica que el token y phone_number_id sean válidos.
    """
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    datos = request.json
    if not datos:
        return jsonify({"error": "No se recibieron datos"}), 400
    
    phone_id_raw = datos.get("phone_number_id")
    access_token_raw = datos.get("access_token")

    if not isinstance(phone_id_raw, str) or not isinstance(access_token_raw, str):
        return jsonify({"error": "Phone Number ID y Access Token son requeridos"}), 400

    phone_id = phone_id_raw.strip()
    access_token = access_token_raw.strip()  # Token nuevo para probar
    
    if not phone_id or not access_token:
        return jsonify({"error": "Phone Number ID y Access Token son requeridos"}), 400

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a base de datos"}), 500

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cliente_id
            FROM tenant_integraciones
            WHERE whatsapp_phone_number_id = %s
              AND cliente_id <> %s
        """, (phone_id, cliente_id))

        if cur.fetchone():
            return jsonify({
                "error": "Este WhatsApp Phone Number ID ya está asociado a otro negocio."
            }), 409
    except Exception as e:
        app.logger.error(f"❌ Error validando propiedad del Phone ID: {str(e)}")
        return jsonify({"error": "Error interno del servidor"}), 500
    finally:
        conn.rollback()
        liberar_db(conn)

    try:
        # Prueba básica: obtener información del número de WhatsApp
        url = f"https://graph.facebook.com/v18.0/{phone_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        resp = requests.get(url, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            return jsonify({
                "ok": True,
                "mensaje": f"✅ Conectado: {data.get('name', 'WhatsApp Business')}",
                "phone_id": phone_id,
                "verified_name": data.get('verified_name'),
                "quality_rating": data.get('quality_rating')
            }), 200
            
        elif resp.status_code == 401:
            return jsonify({"error": "❌ Token inválido o expirado"}), 401
            
        elif resp.status_code == 404:
            return jsonify({"error": f"❌ Phone Number ID no encontrado: {phone_id}"}), 404
            
        else:
            error_detail = resp.text[:200] if resp.text else "Sin detalles"
            return jsonify({
                "error": f"❌ HTTP {resp.status_code}: {error_detail}"
            }), resp.status_code
            
    except requests.Timeout:
        return jsonify({"error": "⏱️ Timeout: no se pudo conectar con Meta en 10s"}), 504
    except requests.ConnectionError:
        return jsonify({"error": "🌐 Error de conexión: verifica tu internet"}), 503
    except Exception as e:
        app.logger.error(f"❌ Error en test_whatsapp_connection: {str(e)}")
        return jsonify({"error": f"❌ Error interno: {str(e)[:100]}"}), 500      
        

# ============================================================================
# WEBHOOK META: WHATSAPP / FACEBOOK / INSTAGRAM (MULTI-TENANT)
# ============================================================================
@app.route("/webhook/meta", methods=["GET", "POST"])
def webhook_meta():
    """
    Endpoint público para recibir mensajes de Meta (WhatsApp, FB, IG).
    - GET: Verificación del webhook por Meta
    - POST: Procesamiento de mensajes entrantes
    """
    # ==================== GET: VERIFICACIÓN DE WEBHOOK ====================
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        VERIFY_TOKEN = os.getenv("META_WEBHOOK_VERIFY_TOKEN", "mi_token_secreto_123")
        
        if mode == "subscribe" and token == VERIFY_TOKEN:
            app.logger.info("✅ Webhook verificado exitosamente por Meta")
            return challenge, 200
        else:
            return "Token inválido", 403

    # ==================== POST: PROCESAMIENTO DE MENSAJES ====================
    try:
        payload = request.json
        if not payload:
            return jsonify({"error": "Payload vacío"}), 400

        # Filtrar solo eventos de WhatsApp o Facebook con mensajes
        if "object" not in payload or payload.get("object") not in ["whatsapp", "page"]:
            return jsonify({"ok": True}), 200

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                
                # Si no hay mensajes, puede ser status update (sent, delivered, read)
                if not messages:
                    continue

                # Identificadores clave
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                external_user_id = messages[0].get("from")
                msg_type = messages[0].get("type")
                msg_text = ""
                
                if msg_type == "text":
                    msg_text = messages[0].get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    # Soporte para botones y listas
                    msg_text = (messages[0].get("interactive", {}).get("button_reply", {}).get("id") or
                               messages[0].get("interactive", {}).get("list_reply", {}).get("id") or
                               "[interactive]")
                else:
                    msg_text = f"[{msg_type}]"

                if not phone_number_id or not external_user_id:
                    continue

                # 🔍 1. IDENTIFICAR TENANT POR phone_number_id (CORREGIDO)
                conn = conectar_db()
                if not conn: 
                    app.logger.error("❌ No se pudo conectar a la BD")
                    continue

                cur = conn.cursor()
                cur.execute("""
                    SELECT 
                        tbc.cliente_id,
                        tbc.bot_activo, 
                        tbc.usar_ia, 
                        tbc.instrucciones_ia, 
                        tbc.modelo_ia, 
                        tbc.temperatura_ia, 
                        tbc.mensaje_fallback, 
                        tbc.handoff_keywords, 
                        tbc.handoff_email,
                        ti.whatsapp_access_token
                    FROM tenant_bot_config tbc
                    JOIN tenant_integraciones ti ON tbc.cliente_id = ti.cliente_id
                    WHERE ti.whatsapp_phone_number_id = %s
                """, (phone_number_id,))

                tenant = cur.fetchone()
                if not tenant:
                    app.logger.warning(f"⚠️ phone_number_id {phone_number_id} no registrado")
                    liberar_db(conn)
                    continue

                # Desempaquetar 10 valores
                (tbc_cliente_id, bot_activo, usar_ia, instrucciones_ia, modelo_ia, 
                 temp_ia, fallback, handoff_kws, handoff_email, access_token_enc) = tenant
                
                if not bot_activo:
                    liberar_db(conn)
                    continue

                # 📝 2. REGISTRAR MENSAJE ENTRANTE
                cur.execute("""
                    INSERT INTO conversation_logs 
                    (cliente_id, external_user_id, platform, direccion, mensaje_texto, mensaje_tipo, creado_en)
                    VALUES (%s, %s, 'whatsapp', 'incoming', %s, %s, CURRENT_TIMESTAMP)
                """, (tbc_cliente_id, external_user_id, msg_text, msg_type))
                conn.commit()

                # 🤖 3. MOTOR DE RESPUESTAS
                respuesta_bot = None
                procesado_por = "fallback"

                # 3a. Handoff a humano
                if handoff_kws and any(kw.lower() in msg_text.lower() for kw in (handoff_kws or [])):
                    respuesta_bot = "👤 Entendido. Un asesor humano te contactará pronto. Gracias por tu paciencia."
                    procesado_por = "handoff"

                # 3b. Keyword match
                if not respuesta_bot:
                    cur.execute("""
                        SELECT respuesta FROM bot_keywords 
                        WHERE cliente_id = %s AND activo = TRUE 
                        AND (LOWER(keyword) = %s OR %s LIKE CONCAT('%', LOWER(keyword), '%'))
                        ORDER BY exact_match DESC LIMIT 1
                    """, (tbc_cliente_id, msg_text.lower(), msg_text.lower()))
                    kw_row = cur.fetchone()
                    if kw_row:
                        respuesta_bot = kw_row[0]
                        procesado_por = "keyword"

                # 3c. Fallback o IA (placeholder)
                if not respuesta_bot:
                    if usar_ia and instrucciones_ia:
                        # TODO: Implementar llamada real a OpenAI
                        respuesta_bot = fallback or "😅 Estoy aprendiendo. Intenta con 'horario' o 'precio'."
                        procesado_por = "ia"
                    else:
                        respuesta_bot = fallback or "😅 No entendí tu mensaje. ¿Puedes reformularlo?"
                        procesado_por = "fallback"

                # 📤 4. ENVIAR RESPUESTA VÍA META API (solo una vez, con token desencriptado)
                if respuesta_bot and access_token_enc:
                    try:
                        access_token = desencriptar_credencial(access_token_enc)
                        
                        if access_token:
                            url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
                            headers = {
                                "Authorization": f"Bearer {access_token}",
                                "Content-Type": "application/json"
                            }
                            payload_reply = {
                                "messaging_product": "whatsapp",
                                "to": external_user_id,
                                "type": "text",
                                "text": {"body": respuesta_bot}
                            }
                            resp = requests.post(url, json=payload_reply, headers=headers, timeout=10)
                            
                            if resp.status_code in (200, 201):
                                app.logger.info(f"📤 Respuesta enviada a {external_user_id}")
                            else:
                                app.logger.error(f"❌ Meta API Error {resp.status_code}: {resp.text[:200]}")
                        else:
                            app.logger.warning("⚠️ No se pudo desencriptar el access token")
                    except Exception as e:
                        app.logger.error(f"❌ Error al enviar respuesta: {e}")

                # 📝 5. REGISTRAR RESPUESTA SALIENTE
                cur.execute("""
                    INSERT INTO conversation_logs 
                    (cliente_id, external_user_id, platform, direccion, mensaje_texto, procesado_por, creado_en)
                    VALUES (%s, %s, 'whatsapp', 'outgoing', %s, %s, CURRENT_TIMESTAMP)
                """, (tbc_cliente_id, external_user_id, respuesta_bot, procesado_por))
                conn.commit()

                # 🔗 6. SOCKET: ACTUALIZAR CHAT EN TIEMPO REAL
                try:
                    socketio.emit("nuevo_mensaje_chat", {
                        "cliente_id": tbc_cliente_id,
                        "external_user_id": external_user_id,
                        "texto": respuesta_bot,
                        "direccion": "outgoing",
                        "timestamp": datetime.now().isoformat()
                    }, room=f"cliente_{tbc_cliente_id}")
                except Exception as e:
                    app.logger.warning(f"⚠️ Socket emit falló: {e}")

                liberar_db(conn)

        return jsonify({"ok": True}), 200

    except Exception as e:
        app.logger.error(f"❌ Error crítico en webhook_meta: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno"}), 500
    
# ============================================================================
# ENDPOINTS: CONFIGURACIÓN DE CHATBOT MULTI-TENANT
# ============================================================================
@app.route("/api/bot/config", methods=["GET", "POST"])
def api_bot_config():
    """
    Obtener o actualizar la configuración general del bot del tenant actual.
    GET: Retorna config actual (enmascarada si es sensible)
    POST: Guarda/actualiza config con encriptación si aplica
    """
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a base de datos"}), 500
    
    try:
        cur = conn.cursor()
        
        if request.method == "GET":
            # 🔍 Obtener configuración actual del tenant
            cur.execute("""
                SELECT bot_activo, nombre_bot, mensaje_bienvenida, mensaje_fallback, 
                       usar_ia, instrucciones_ia, modelo_ia, temperatura_ia, 
                       handoff_keywords, handoff_email, actualizado_en
                FROM tenant_bot_config 
                WHERE cliente_id = %s
            """, (cliente_id,))
            
            row = cur.fetchone()
            
            if row:
                return jsonify({
                    "bot_activo": row[0] or False,
                    "nombre_bot": row[1] or "Asistente Virtual",
                    "mensaje_bienvenida": row[2] or "👋 ¡Hola! ¿En qué puedo ayudarte?",
                    "mensaje_fallback": row[3] or "😅 No entendí tu mensaje. ¿Puedes reformularlo?",
                    "usar_ia": row[4] or False,
                    "instrucciones_ia": row[5],  # Puede ser None
                    "modelo_ia": row[6] or "gpt-3.5-turbo",
                    "temperatura_ia": row[7] or 0.7,
                    "handoff_keywords": row[8] or [],  # Array de texto
                    "handoff_email": row[9],  # Puede ser None
                    "actualizado_en": row[10].isoformat() if row[10] else None
                }), 200
            else:
                # No hay config aún → retornar defaults
                return jsonify({
                    "bot_activo": False,
                    "nombre_bot": "Asistente Virtual",
                    "mensaje_bienvenida": "👋 ¡Hola! ¿En qué puedo ayudarte?",
                    "mensaje_fallback": "😅 No entendí tu mensaje. ¿Puedes reformularlo?",
                    "usar_ia": False,
                    "instrucciones_ia": None,
                    "modelo_ia": "gpt-3.5-turbo",
                    "temperatura_ia": 0.7,
                    "handoff_keywords": [],
                    "handoff_email": None,
                    "existe": False
                }), 200
                
        else:  # POST - Guardar/Actualizar configuración
            data = request.json
            if not data:
                return jsonify({"error": "No se recibieron datos"}), 400
            
            # Validar campos requeridos mínimos
            if "nombre_bot" not in data:
                return jsonify({"error": "El nombre del bot es requerido"}), 400
            
            # Preparar valores para BD (manejar arrays y nulls)
            handoff_keywords = data.get("handoff_keywords")
            if isinstance(handoff_keywords, list):
                handoff_keywords = [k.strip() for k in handoff_keywords if k.strip()]
            elif handoff_keywords and isinstance(handoff_keywords, str):
                # Si viene como string separado por comas, convertir a lista
                handoff_keywords = [k.strip() for k in handoff_keywords.split(",") if k.strip()]
            else:
                handoff_keywords = []
            
            # 🔗 INSERT o UPDATE con ON CONFLICT
            cur.execute("""
                INSERT INTO tenant_bot_config 
                (cliente_id, bot_activo, nombre_bot, mensaje_bienvenida, mensaje_fallback, 
                 usar_ia, instrucciones_ia, modelo_ia, temperatura_ia, handoff_keywords, handoff_email)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cliente_id) DO UPDATE SET 
                    bot_activo = EXCLUDED.bot_activo,
                    nombre_bot = EXCLUDED.nombre_bot,
                    mensaje_bienvenida = EXCLUDED.mensaje_bienvenida,
                    mensaje_fallback = EXCLUDED.mensaje_fallback,
                    usar_ia = EXCLUDED.usar_ia,
                    instrucciones_ia = EXCLUDED.instrucciones_ia,
                    modelo_ia = EXCLUDED.modelo_ia,
                    temperatura_ia = EXCLUDED.temperatura_ia,
                    handoff_keywords = EXCLUDED.handoff_keywords,
                    handoff_email = EXCLUDED.handoff_email,
                    actualizado_en = CURRENT_TIMESTAMP
            """, (
                cliente_id,
                data.get("bot_activo", False),
                data.get("nombre_bot", "Asistente Virtual"),
                data.get("mensaje_bienvenida", "👋 ¡Hola! ¿En qué puedo ayudarte?"),
                data.get("mensaje_fallback", "😅 No entendí tu mensaje. ¿Puedes reformularlo?"),
                data.get("usar_ia", False),
                data.get("instrucciones_ia"),
                data.get("modelo_ia", "gpt-3.5-turbo"),
                data.get("temperatura_ia", 0.7),
                handoff_keywords,
                data.get("handoff_email")
            ))
            
            conn.commit()
            
            # 🔗 EMITIR SOCKET: Config general actualizada (tiempo real)
            try:
                socketio.emit("configuracion_actualizada", {
                    "tipo": "chatbot",
                    "subtipo": "config",
                    "cliente_id": cliente_id,
                    "timestamp": datetime.now().isoformat(),
                    "mensaje": "Configuración general actualizada"
                }, room=f"cliente_{cliente_id}")
                app.logger.info(f"🔗 Socket emitido: chatbot config actualizada para cliente {cliente_id}")
            except Exception as e:
                app.logger.warning(f"⚠️ No se pudo emitir socket para chatbot config: {e}")
                # No fallar la petición si el socket falla
            
            return jsonify({
                "ok": True, 
                "mensaje": "✅ Configuración del bot actualizada correctamente",
                "cliente_id": cliente_id
            }), 200
            
    except Exception as e:
        conn.rollback()
        app.logger.error(f"❌ Error en api_bot_config: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Error interno: {str(e)[:100]}"}), 500
    finally:
        liberar_db(conn)
            
# ============================================================================
# ENDPOINT: GESTIÓN DE KEYWORDS (RESPUESTAS RÁPIDAS) POR TENANT
# ============================================================================
@app.route("/api/bot/keywords", methods=["GET", "POST", "DELETE"])
def api_bot_keywords():
    """
    CRUD de respuestas rápidas por palabra clave para el tenant actual.
    GET: Lista todas las keywords activas/inactivas
    POST: Crea o actualiza una keyword
    DELETE: Elimina una keyword por ID
    """
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión a base de datos"}), 500
    
    try:
        cur = conn.cursor()
        
        # ==================== GET: Listar keywords ====================
        if request.method == "GET":
            cur.execute("""
                SELECT id, keyword, respuesta, exact_match, case_sensitive, 
                       veces_usada, activo, creado_en, actualizado_en
                FROM bot_keywords 
                WHERE cliente_id = %s 
                ORDER BY keyword ASC
            """, (cliente_id,))
            
            keywords = []
            for row in cur.fetchall():
                keywords.append({
                    "id": row[0],
                    "keyword": row[1],
                    "respuesta": row[2],
                    "exact_match": row[3],
                    "case_sensitive": row[4],
                    "veces_usada": row[5],
                    "activo": row[6],
                    "creado_en": row[7].isoformat() if row[7] else None,
                    "actualizado_en": row[8].isoformat() if row[8] else None
                })
            
            return jsonify(keywords), 200
        
        # ==================== POST: Crear/Actualizar keyword ====================
        elif request.method == "POST":
            data = request.json
            if not data:
                return jsonify({"error": "No se recibieron datos"}), 400
            
            keyword = data.get("keyword", "").strip().lower()
            respuesta = data.get("respuesta", "").strip()
            
            if not keyword or not respuesta:
                return jsonify({"error": "Keyword y respuesta son requeridos"}), 400
            
            # Validar longitud máxima
            if len(keyword) > 100:
                return jsonify({"error": "La keyword no puede exceder 100 caracteres"}), 400
            
            # 🔗 INSERT o UPDATE con ON CONFLICT
            cur.execute("""
                INSERT INTO bot_keywords 
                (cliente_id, keyword, respuesta, exact_match, case_sensitive, activo)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (cliente_id, keyword) DO UPDATE SET 
                    respuesta = EXCLUDED.respuesta,
                    exact_match = EXCLUDED.exact_match,
                    case_sensitive = EXCLUDED.case_sensitive,
                    activo = TRUE,
                    actualizado_en = CURRENT_TIMESTAMP
            """, (
                cliente_id,
                keyword,
                respuesta,
                data.get("exact_match", False),
                data.get("case_sensitive", False)
            ))
            
            conn.commit()
            
            # 🔗 EMITIR SOCKET: Keywords actualizadas (tiempo real)
            try:
                socketio.emit("configuracion_actualizada", {
                    "tipo": "chatbot",
                    "subtipo": "keywords",
                    "cliente_id": cliente_id,
                    "timestamp": datetime.now().isoformat(),
                    "keyword": keyword,
                    "accion": "creada_o_actualizada"
                }, room=f"cliente_{cliente_id}")
                app.logger.info(f"🔗 Socket emitido: keyword '{keyword}' actualizada para cliente {cliente_id}")
            except Exception as e:
                app.logger.warning(f"⚠️ No se pudo emitir socket para keywords: {e}")
            
            return jsonify({
                "ok": True, 
                "mensaje": f"✅ Keyword '{keyword}' guardada correctamente",
                "id": cur.lastrowid if cur.lastrowid else None
            }), 200
        
        # ==================== DELETE: Eliminar keyword ====================
        elif request.method == "DELETE":
            keyword_id = request.args.get("id", type=int)
            
            if not keyword_id:
                return jsonify({"error": "ID de keyword es requerido"}), 400
            
            # Verificar que la keyword pertenece al tenant antes de eliminar
            cur.execute("""
                SELECT keyword FROM bot_keywords 
                WHERE id = %s AND cliente_id = %s
            """, (keyword_id, cliente_id))
            
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Keyword no encontrada o no pertenece a este tenant"}), 404
            
            keyword_eliminada = row[0]
            
            cur.execute("""
                DELETE FROM bot_keywords 
                WHERE id = %s AND cliente_id = %s
            """, (keyword_id, cliente_id))
            
            conn.commit()
            
            # 🔗 EMITIR SOCKET: Keywords actualizadas (tiempo real)
            try:
                socketio.emit("configuracion_actualizada", {
                    "tipo": "chatbot",
                    "subtipo": "keywords",
                    "cliente_id": cliente_id,
                    "timestamp": datetime.now().isoformat(),
                    "keyword": keyword_eliminada,
                    "accion": "eliminada"
                }, room=f"cliente_{cliente_id}")
                app.logger.info(f"🔗 Socket emitido: keyword '{keyword_eliminada}' eliminada para cliente {cliente_id}")
            except Exception as e:
                app.logger.warning(f"⚠️ No se pudo emitir socket para keywords: {e}")
            
            return jsonify({
                "ok": True, 
                "mensaje": f"✅ Keyword '{keyword_eliminada}' eliminada correctamente",
                "eliminadas": cur.rowcount
            }), 200
            
    except Exception as e:
        conn.rollback()
        app.logger.error(f"❌ Error en api_bot_keywords: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Error interno: {str(e)[:100]}"}), 500
    finally:
        liberar_db(conn)
        

# ============================================================================
# ENDPOINT: GESTIÓN DE FLOWS - VERSIÓN CORREGIDA PARA JSONB
# ============================================================================
@app.route("/api/bot/flows", methods=["GET", "POST", "PUT", "DELETE"])
def api_bot_flows():
    """CRUD de flujos de conversación con pasos JSONB"""
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500
    
    try:
        cur = conn.cursor()
        
        # ==================== GET: Listar flujos ====================
        if request.method == "GET":
            cur.execute("""
                SELECT id, nombre, descripcion, trigger_keyword, trigger_type, 
                       pasos, requiere_autenticacion, timeout_segundos, 
                       max_reintentos, activo, orden, creado_en, actualizado_en
                FROM bot_flows WHERE cliente_id = %s ORDER BY orden ASC, nombre ASC
            """, (cliente_id,))
            
            flows = []
            for row in cur.fetchall():
                flows.append({
                    "id": row[0], "nombre": row[1], "descripcion": row[2],
                    "trigger_keyword": row[3], "trigger_type": row[4],
                    "pasos": row[5],  # psycopg2 convierte JSONB → dict automáticamente en SELECT
                    "requiere_autenticacion": row[6], "timeout_segundos": row[7],
                    "max_reintentos": row[8], "activo": row[9], "orden": row[10],
                    "creado_en": row[11].isoformat() if row[11] else None,
                    "actualizado_en": row[12].isoformat() if row[12] else None
                })
            return jsonify(flows), 200
        
        # ==================== POST: Crear/Actualizar flujo ====================
        elif request.method == "POST":
            import json  # ← Agregar al inicio del archivo si no está
            
            data = request.json
            if not data:
                return jsonify({"error": "No se recibieron datos"}), 400
            
            nombre = data.get("nombre", "").strip()
            if not nombre:
                return jsonify({"error": "El nombre del flujo es requerido"}), 400
            
            # 🔧 VALIDAR Y CONVERTIR pasos a JSON string para PostgreSQL
            pasos = data.get("pasos")
            if not pasos:
                return jsonify({"error": "Los pasos del flujo son requeridos"}), 400
            
            # Si es dict/list Python, convertir a string JSON
            if isinstance(pasos, (dict, list)):
                pasos_json = json.dumps(pasos, ensure_ascii=False)
            elif isinstance(pasos, str):
                # Si ya es string, validar que sea JSON válido
                try:
                    json.loads(pasos)  # Solo validar
                    pasos_json = pasos
                except json.JSONDecodeError as e:
                    return jsonify({"error": f"JSON inválido en pasos: {str(e)}"}), 400
            else:
                return jsonify({"error": "Los pasos deben ser un objeto o array JSON"}), 400
            
            # 🔧 USAR pasos_json (string) en la consulta, NO el dict original
            cur.execute("""
                INSERT INTO bot_flows 
                (cliente_id, nombre, descripcion, trigger_keyword, trigger_type, 
                 pasos, requiere_autenticacion, timeout_segundos, max_reintentos, activo, orden)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, TRUE, %s)
                ON CONFLICT (cliente_id, nombre) DO UPDATE SET 
                    descripcion = EXCLUDED.descripcion,
                    trigger_keyword = EXCLUDED.trigger_keyword,
                    trigger_type = EXCLUDED.trigger_type,
                    pasos = EXCLUDED.pasos::jsonb,
                    requiere_autenticacion = EXCLUDED.requiere_autenticacion,
                    timeout_segundos = EXCLUDED.timeout_segundos,
                    max_reintentos = EXCLUDED.max_reintentos,
                    activo = TRUE,
                    orden = EXCLUDED.orden,
                    actualizado_en = CURRENT_TIMESTAMP
            """, (
                cliente_id, nombre, data.get("descripcion"),
                data.get("trigger_keyword"), data.get("trigger_type", "keyword"),
                pasos_json,  # ← String JSON, NO dict
                data.get("requiere_autenticacion", False),
                data.get("timeout_segundos", 300),
                data.get("max_reintentos", 3),
                data.get("orden", 0)
            ))
            
            conn.commit()
            
            # 🔗 Socket emit (igual que antes)
            try:
                socketio.emit("configuracion_actualizada", {
                    "tipo": "chatbot", "subtipo": "flows",
                    "cliente_id": cliente_id, "timestamp": datetime.now().isoformat(),
                    "flow_nombre": nombre, "accion": "creado_o_actualizado"
                }, room=f"cliente_{cliente_id}")
            except Exception as e:
                app.logger.warning(f"⚠️ Socket emit falló: {e}")
            
            return jsonify({
                "ok": True, 
                "mensaje": f"✅ Flujo '{nombre}' guardado correctamente",
                "id": cur.lastrowid if cur.lastrowid else None
            }), 200
        
        # ==================== DELETE: Eliminar flujo ====================
        elif request.method == "DELETE":
            flow_id = request.args.get("id", type=int)
            if not flow_id:
                return jsonify({"error": "ID de flujo es requerido"}), 400
            
            cur.execute("SELECT nombre FROM bot_flows WHERE id = %s AND cliente_id = %s", (flow_id, cliente_id))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Flujo no encontrado"}), 404
            
            flow_nombre = row[0]
            cur.execute("DELETE FROM bot_flows WHERE id = %s AND cliente_id = %s", (flow_id, cliente_id))
            conn.commit()
            
            # 🔗 Socket emit para delete
            try:
                socketio.emit("configuracion_actualizada", {
                    "tipo": "chatbot", "subtipo": "flows",
                    "cliente_id": cliente_id, "timestamp": datetime.now().isoformat(),
                    "flow_nombre": flow_nombre, "accion": "eliminado"
                }, room=f"cliente_{cliente_id}")
            except Exception as e:
                app.logger.warning(f"⚠️ Socket emit falló: {e}")
            
            return jsonify({"ok": True, "mensaje": f"✅ Flujo eliminado", "eliminados": cur.rowcount}), 200
            
            
                    # ==================== PUT: Actualizar flujo existente ====================
        elif request.method == "PUT":
            import json
            
            flow_id = request.args.get("id", type=int)
            if not flow_id:
                return jsonify({"error": "ID de flujo es requerido para actualizar"}), 400
            
            data = request.json
            if not data:
                return jsonify({"error": "No se recibieron datos"}), 400
            
            # Validar que el flujo existe y pertenece al tenant
            cur.execute("SELECT nombre FROM bot_flows WHERE id = %s AND cliente_id = %s", (flow_id, cliente_id))
            if not cur.fetchone():
                return jsonify({"error": "Flujo no encontrado o no autorizado"}), 404
            
            # Procesar pasos JSON (igual que en POST)
            pasos = data.get("pasos")
            if isinstance(pasos, (dict, list)):
                pasos_json = json.dumps(pasos, ensure_ascii=False)
            elif isinstance(pasos, str):
                try:
                    json.loads(pasos)
                    pasos_json = pasos
                except json.JSONDecodeError as e:
                    return jsonify({"error": f"JSON inválido: {str(e)}"}), 400
            else:
                return jsonify({"error": "Los pasos deben ser JSON válido"}), 400
            
            # UPDATE explícito
            cur.execute("""
                UPDATE bot_flows SET 
                    nombre = %s,
                    descripcion = %s,
                    trigger_keyword = %s,
                    trigger_type = %s,
                    pasos = %s::jsonb,
                    requiere_autenticacion = %s,
                    timeout_segundos = %s,
                    max_reintentos = %s,
                    orden = %s,
                    actualizado_en = CURRENT_TIMESTAMP
                WHERE id = %s AND cliente_id = %s
            """, (
                data.get("nombre"), data.get("descripcion"),
                data.get("trigger_keyword"), data.get("trigger_type", "keyword"),
                pasos_json,
                data.get("requiere_autenticacion", False),
                data.get("timeout_segundos", 300),
                data.get("max_reintentos", 3),
                data.get("orden", 0),
                flow_id, cliente_id
            ))
            
            conn.commit()
            
            # 🔗 Socket emit
            try:
                socketio.emit("configuracion_actualizada", {
                    "tipo": "chatbot", "subtipo": "flows",
                    "cliente_id": cliente_id, "timestamp": datetime.now().isoformat(),
                    "flow_nombre": data.get("nombre"), "accion": "actualizado"
                }, room=f"cliente_{cliente_id}")
            except Exception as e:
                app.logger.warning(f"⚠️ Socket emit falló: {e}")
            
            return jsonify({
                "ok": True, 
                "mensaje": "✅ Flujo actualizado correctamente",
                "id": flow_id
            }), 200
            
            
    except Exception as e:
        conn.rollback()
        app.logger.error(f"❌ Error en api_bot_flows: {str(e)}")
        return jsonify({"error": f"Error interno: {str(e)[:100]}"}), 500
    finally:
        liberar_db(conn)

# ============================================================================

#Verificar CODIGO DE SEGURIDAD
@app.route("/verificar_codigo_seguridad", methods=["POST"])
def verificar_codigo_seguridad():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"valido": False}), 401

    datos = request.json
    codigo = datos.get("codigo", "")
    
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("SELECT codigo_seguridad FROM clientes WHERE id = %s", (cliente_id,))
    resultado = cur.fetchone()
    liberar_db(conn)
    
    if resultado and resultado[0] == codigo:
        return jsonify({"valido": True})
    else:
        return jsonify({"valido": False})
    
# Actualizar codigo de seguridad    
@app.route("/actualizar_codigo_seguridad", methods=["POST"])
def actualizar_codigo_seguridad():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401

    datos = request.json
    codigo = datos.get("codigo", "")
    
    if not re.match(r'^\d{4}$', codigo):
        return jsonify({"error": "Código inválido"}), 400
    
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("UPDATE clientes SET codigo_seguridad = %s WHERE id = %s", (codigo, cliente_id))
    conn.commit()
    liberar_db(conn)
    
    return jsonify({"ok": True})


# 📌 Endpoint para obtener campos del tenant
@app.route("/campos_evento", methods=["GET"])
def obtener_campos_evento():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify([]), 401
    
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT nombre, clave, tipo, opciones, obligatorio
        FROM campos_evento_tenant
        WHERE cliente_id = %s AND activo = true
        ORDER BY orden
    """, (cliente_id,))
    campos = [
        {
            "nombre": row[0],
            "clave": row[1],
            "tipo": row[2],
            "opciones": row[3].split(",") if row[3] else [],
            "obligatorio": row[4]
        }
        for row in cur.fetchall()
    ]
    liberar_db(conn)
    return jsonify(campos)


# 📌 Endpoint para guardar campos del tenant
@app.route("/campos_evento", methods=["POST"])
def guardar_campos_evento():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    
    campos = request.json.get("campos", [])
    if not isinstance(campos, list):
        return jsonify({"error": "Formato inválido"}), 400
    
    # ✅ VALIDAR CLAVES ÚNICAS
    claves = [c.get("clave", "").strip().lower() for c in campos if c.get("clave")]
    if len(claves) != len(set(claves)):
        return jsonify({"error": "Hay claves duplicadas"}), 400
    
    # ✅ VALIDAR QUE TODOS TENGAN NOMBRE Y CLAVE
    for i, campo in enumerate(campos):
        if not campo.get("nombre", "").strip():
            return jsonify({"error": f"El campo {i+1} no tiene nombre"}), 400
        if not campo.get("clave", "").strip():
            return jsonify({"error": f"El campo {i+1} no tiene clave"}), 400
    
    conn = conectar_db()
    cur = conn.cursor()
    
    # Eliminar campos anteriores
    cur.execute("DELETE FROM campos_evento_tenant WHERE cliente_id = %s", (cliente_id,))
    
    # Insertar nuevos
    for i, campo in enumerate(campos):
        nombre = campo.get("nombre", "").strip()
        clave = campo.get("clave", "").strip().lower().replace(" ", "_")
        tipo = campo.get("tipo", "text")
        opciones = ",".join(campo.get("opciones", [])) if campo.get("opciones") else None
        obligatorio = bool(campo.get("obligatorio", False))
        
        if nombre and clave:
            cur.execute("""
                INSERT INTO campos_evento_tenant
                (cliente_id, nombre, clave, tipo, opciones, obligatorio, orden, activo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, true)
            """, (cliente_id, nombre, clave, tipo, opciones, obligatorio, i))
    
    conn.commit()
    liberar_db(conn)
    
    # ✅ EMITIR EVENTO SOCKET PARA ACTUALIZACIÓN EN TIEMPO REAL
    socketio.emit(
        "configuracion_actualizada",
        {
            "tipo": "campos_evento",
            "cliente_id": cliente_id,
            "timestamp": datetime.now().isoformat(),
            "cantidad_campos": len(campos)
        },
        room=f"cliente_{cliente_id}"
    )
    
    return jsonify({"ok": True})



@app.route("/servicios", methods=["GET"])
def obtener_servicios_tenant():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify([]), 401
    
    conn = conectar_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT nombre, clave, tipo 
        FROM servicios_tenant 
        WHERE cliente_id = %s AND activo = true
        ORDER BY nombre
    """, (cliente_id,))
    servicios = [{"nombre": r[0], "clave": r[1], "tipo": r[2]} for r in cur.fetchall()]
    liberar_db(conn)
    return jsonify(servicios)


# Actualiza la función guardar_servicios_tenant en Pasted_Text_1773946908608.txt
@app.route("/servicios", methods=["POST"])
def guardar_servicios_tenant():
    cliente_id = obtener_cliente_id_de_subdominio()
    if not cliente_id:
        return jsonify({"error": "No autorizado"}), 401
    servicios = request.json.get("servicios", [])
    if not isinstance(servicios, list):
        return jsonify({"error": "Formato inválido"}), 400
    conn = conectar_db()
    cur = conn.cursor()
    # Eliminar servicios anteriores
    cur.execute("DELETE FROM servicios_tenant WHERE cliente_id = %s", (cliente_id,))
    # Insertar nuevos
    for serv in servicios:
        nombre = serv.get("nombre", "").strip()
        clave = serv.get("clave", "").strip().lower().replace(" ", "_")
        tipo = serv.get("tipo", "boolean")
        if nombre and clave:
            cur.execute("""
            INSERT INTO servicios_tenant (cliente_id, nombre, clave, tipo)
            VALUES (%s, %s, %s, %s)
            """, (cliente_id, nombre, clave, tipo))
    conn.commit()
    liberar_db(conn)
    
    # ✅ EMITIR EVENTO SOCKET PARA ACTUALIZACIÓN EN TIEMPO REAL
    socketio.emit(
        "configuracion_actualizada",
        {
            "tipo": "servicios",
            "cliente_id": cliente_id,
            "timestamp": datetime.now().isoformat()
        },
        room=f"cliente_{cliente_id}"
    )
    
    return jsonify({"ok": True})



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
        
# Verificar que el tenant exisata antes de redirigir a la URL correspondiente
@app.route("/verificar-tenant", methods=["POST"])
def verificar_tenant():
    """Verifica si un tenant existe"""
    try:
        datos = request.json
        subdominio = datos.get("subdominio", "").strip().lower()
        
        if not subdominio:
            return jsonify({"error": "Subdominio requerido"}), 400
        
        conn = conectar_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM clientes WHERE subdominio = %s AND activo = true", (subdominio,))
        existe = cur.fetchone() is not None
        liberar_db(conn)
        
        return jsonify({"existe": existe})
        
    except Exception as e:
        print(f"Error al verificar tenant: {str(e)}")
        return jsonify({"error": "Error interno"}), 500
    
    

@app.route("/registro")
def pagina_registro():
    """
    Página de registro para nuevos clientes.
    Accesible desde crm.eventa.com.mx y registro.eventa.com.mx
    """
    host = request.host
    if host not in ["crm.eventa.com.mx"]:
        # Opcional: redirigir a crm.eventa.com.mx si viene de otro lugar
        return redirect("https://crm.eventa.com.mx/registro")
    
    return render_template("registro.html")
 

# A. Registrar usuario (sin contraseña aún)
@app.route("/registro", methods=["POST"])
def procesar_registro():
    try:
        datos = request.json
        nombre = datos.get("nombre", "").strip()
        subdominio = datos.get("subdominio", "").strip().lower()
        email = datos.get("email", "").strip().lower()
        plan = datos.get("plan", "basico")
        
        # Validaciones
        if not nombre or not subdominio or not email:
            return jsonify({"error": "Todos los campos son requeridos"}), 400
        if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', subdominio):
            return jsonify({"error": "Subdominio inválido"}), 400
        if len(subdominio) < 3 or len(subdominio) > 30:
            return jsonify({"error": "El subdominio debe tener entre 3 y 30 caracteres"}), 400
        if not email or '@' not in email:
            return jsonify({"error": "Email inválido"}), 400
        if plan not in ['basico', 'premium']:
            return jsonify({"error": "Plan inválido"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión"}), 500

        try:
            cur = conn.cursor()
            
            # Verificar si el subdominio ya existe
            cur.execute("SELECT id FROM clientes WHERE subdominio = %s", (subdominio,))
            if cur.fetchone():
                return jsonify({"error": "Este subdominio ya está en uso"}), 400
                
            # Verificar si el email ya está registrado
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return jsonify({"error": "Este email ya está registrado"}), 400
 
            # Generar código de verificación
            codigo_verificacion = secrets.token_urlsafe(6)[:8]
            expiracion = datetime.utcnow() + timedelta(hours=1)

            # ✅ CORREGIDO: Usar diccionario de parámetros
            query_cliente = """
                INSERT INTO clientes (
                    nombre, subdominio, plan, activo, 
                    email_verificado, codigo_verificacion, 
                    codigo_expiracion, email_admin
                )
                VALUES (%(nombre)s, %(subdominio)s, %(plan)s, %(activo)s, 
                        %(email_verificado)s, %(codigo_verificacion)s, 
                        %(codigo_expiracion)s, %(email_admin)s)
                RETURNING id
            """

            params_cliente = {
                'nombre': nombre,
                'subdominio': subdominio,
                'plan': plan,
                'activo': True,
                'email_verificado': False,
                'codigo_verificacion': codigo_verificacion,
                'codigo_expiracion': expiracion,
                'email_admin': email
            }

            cur.execute(query_cliente, params_cliente)
            cliente_id = cur.fetchone()[0]
            print(f"✅ DEBUG: Cliente creado con ID: {cliente_id}")

            # Crear usuario
            query_usuario = """
                INSERT INTO users (email, cliente_id, activo)
                VALUES (%(email)s, %(cliente_id)s, %(activo)s)
                RETURNING id
            """

            params_usuario = {
                'email': email,
                'cliente_id': cliente_id,
                'activo': True
            }

            cur.execute(query_usuario, params_usuario)
            user_id = cur.fetchone()[0]
            print(f"✅ DEBUG: Usuario creado con ID: {user_id}")

            # Asignar rol admin
            cur.execute("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT %(user_id)s, id FROM roles WHERE name = 'admin'
            """, {'user_id': user_id})
            print("✅ DEBUG: Rol admin asignado")
            
            conn.commit()
            # Enviar email (intento)
            email_enviado = enviar_email_verificacion(email, subdominio, codigo_verificacion)

            # 🔍 TEMPORAL: Mostrar código SIEMPRE (hasta resolver SendGrid)
            if not email_enviado:
                print(f"⚠️ EMAIL NO ENVIADO - CÓDIGO MANUAL: {codigo_verificacion}")

            return jsonify({
                "mensaje": "Verifica tu email para completar el registro",
                "subdominio": subdominio,
                "codigo_debug": codigo_verificacion  # ✅ SIEMPRE visible
            }), 200
             
            
            # Enviar email con código de send grid
            #enviar_email_verificacion(email, subdominio, codigo_verificacion)
            
            #return jsonify({
            #    "mensaje": "Verifica tu email para completar el registro",
            #    "subdominio": subdominio
            #}), 200
            
        except Exception as e:
            conn.rollback()
            print(f"❌ Error en procesar_registro: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({"error": "Error al crear el cliente"}), 500
        finally:
            liberar_db(conn)
            
    except Exception as e:
        print(f"💥 ERROR CRÍTICO EN REGISTRO: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno"}), 500
    
    
# B. Verificar código y establecer contraseña
@app.route("/verificar-registro", methods=["POST"])
def verificar_registro():
    try:
        datos = request.json
        subdominio = datos.get("subdominio")
        codigo = datos.get("codigo")
        password = datos.get("password")
        
        if not subdominio or not codigo or not password:
            return jsonify({"error": "Datos incompletos"}), 400
        if len(password) < 6:
            return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400

        conn = conectar_db()
        cur = conn.cursor()
        
        # Buscar cliente por subdominio
        cur.execute("""
            SELECT id, codigo_verificacion, codigo_expiracion, email_verificado
            FROM clientes 
            WHERE subdominio = %s
        """, (subdominio,))
        
        cliente = cur.fetchone()
        if not cliente:
            return jsonify({"error": "Cliente no encontrado"}), 404
            
        cliente_id, codigo_guardado, expiracion, verificado = cliente
        
        if verificado:
            return jsonify({"error": "Email ya verificado"}), 400
            
        if datetime.utcnow() > expiracion:
            return jsonify({"error": "Código expirado"}), 400
            
        if codigo != codigo_guardado:
            return jsonify({"error": "Código inválido"}), 400

        # Actualizar cliente como verificado
        password_hash = generate_password_hash(password)
        cur.execute("""
            UPDATE clientes 
            SET email_verificado = true, codigo_verificacion = NULL, codigo_expiracion = NULL
            WHERE id = %s
        """, (cliente_id,))
        
        # Actualizar contraseña del usuario
        cur.execute("""
            UPDATE users 
            SET password_hash = %s 
            WHERE cliente_id = %s
        """, (password_hash, cliente_id))
        
        conn.commit()
        liberar_db(conn)
        
        return jsonify({
            "mensaje": "Registro completado exitosamente",
            "url": f"https://{subdominio}.eventa.com.mx/login"
        }), 200
        
    except Exception as e:
        print(f"❌ Error en verificar_registro: {str(e)}")
        return jsonify({"error": "Error al verificar registro"}), 500
    
    
# C. Función de envío de email
def enviar_email_verificacion(email_destino, subdominio, codigo):
    try:
        message = Mail(
            from_email=os.getenv('SENDGRID_FROM_EMAIL'),
            to_emails=email_destino,
            subject='Verifica tu cuenta - Eventa CRM',
            html_content=f'''
            <h2>🔐 Código de verificación</h2>
            <p>Hola,</p>
            <p>Gracias por registrarte en Eventa CRM.</p>
            <p><strong>Tu código de verificación es:</strong></p>
            <h1 style="font-size: 32px; color: #3498db;">{codigo}</h1>
            <p>Ingresa este código en el formulario de verificación para completar tu registro.</p>
            <p>Este código expira en 1 hora.</p>
            <hr>
            <p><small>Equipo Eventa CRM</small></p>
            '''
        )
        
        sg = SendGridAPIClient(os.getenv('SENDGRID_API_KEY'))
        response = sg.send(message)
        print(f"✅ Email de verificación enviado a {email_destino}")
        return True
        
    except Exception as e:
        print(f"❌ Error al enviar email de verificación: {str(e)}")
        return False
    
# Actualizar tu ruta de registro
@app.route("/verificar-registro")
def pagina_verificar_registro():
    return render_template("verificar_registro.html")




@app.route("/login")
def pagina_login():
    """
    Página de login para cualquier subdominio.
    El frontend (JS) detecta el subdominio y renderiza el contenido apropiado.
    """
    # ✅ Siempre retornar login.html, el frontend se encarga del resto
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def procesar_login():
    """
    Procesa el login y valida que el usuario pertenezca al cliente actual.
    En modo local (localhost), permite entrar a cualquier tenant para pruebas.
    """
    try:
        cliente_id = obtener_cliente_id_de_subdominio()
        
        # ✅ OBTENER DATOS: Intentar JSON primero, luego fallback a form
        if request.is_json:
            datos = request.get_json()
        else:
            try:
                import json
                datos = json.loads(request.data.decode('utf-8'))
            except:
                datos = request.form.to_dict()
        
        email = datos.get("email", "").strip().lower()
        password = datos.get("password", "")

        if not email or not password:
            return jsonify({"error": "Email y contraseña son requeridos"}), 400

        conn = conectar_db()
        if not conn:
            return jsonify({"error": "Error de conexión"}), 500

        try:
            cur = conn.cursor()
            
            # 🚀 MODO LOCAL: Si estamos en localhost, buscar por email sin importar el subdominio
            if 'localhost' in request.host or '127.0.0.1' in request.host:
                cur.execute("""
                    SELECT id, password_hash, cliente_id 
                    FROM users 
                    WHERE email = %s AND activo = true
                """, (email,))
                user = cur.fetchone()
                if user:
                    # Usar el cliente_id REAL del usuario para la sesión
                    cliente_id = user[2] 
            else:
                # 🌐 MODO PRODUCCIÓN: Validar estrictamente que pertenezca al subdominio
                if not cliente_id:
                    return jsonify({"error": "Cliente no encontrado"}), 404
                cur.execute("""
                    SELECT id, password_hash, cliente_id 
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
            print(f"❌ Error en login (base de datos): {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({"error": "Error interno"}), 500
        finally:
            liberar_db(conn)
            
    except Exception as e:
        print(f"💥 ERROR CRÍTICO EN LOGIN: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Error interno"}), 500
    
    
    
    
        
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
        # Verificar que las variables de entorno existan
        sendgrid_api_key = os.getenv('SENDGRID_API_KEY')
        sendgrid_from_email = os.getenv('SENDGRID_FROM_EMAIL')
        
        if not sendgrid_api_key or not sendgrid_from_email:
            print("❌ Variables de SendGrid no configuradas")
            return False

        message = Mail (
            from_email=sendgrid_from_email,
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
        
        sg = SendGridAPIClient(sendgrid_api_key)
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

    # Obtener cliente_id del subdominio
    cliente_id = obtener_cliente_id_de_subdominio()
    
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500

    try:
        cur = conn.cursor()
        
        if cliente_id:
            # Caso normal: subdominio específico (camicam.eventa.com.mx)
            cur.execute("SELECT id FROM users WHERE email = %s AND cliente_id = %s", (email, cliente_id))
            user = cur.fetchone()
        else:
            # Caso especial: crm.eventa.com.mx - buscar en todos los clientes
            cur.execute("SELECT id, cliente_id FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            if user:
                # Si encontramos el usuario, usamos su cliente_id real
                cliente_id = user[1]  # El segundo campo es cliente_id
        
        if not user:
            # No revelar si el email existe o no (seguridad)
            return jsonify({"mensaje": "Si el email existe, recibirás instrucciones"}), 200

        # Generar token de recuperación
        token = secrets.token_urlsafe(32)
        expiracion = datetime.utcnow() + timedelta(hours=1)  # Válido por 1 hora
        
        
        cur.execute("""
            UPDATE users 
            SET reset_token = %s, reset_expiracion = %s 
            WHERE email = %s AND cliente_id = %s
        """, (token, expiracion, email, cliente_id))
        
        conn.commit()
        
        # Generar URL de recuperación
        if cliente_id:
            # Construir la URL con el subdominio correcto
            # Necesitas una forma de obtener el subdominio del cliente
            # Por ahora, usamos el host actual
            reset_url = f"https://{request.host}/restablecer_password?token={token}"
        else:
            reset_url = f"https://{request.host}/restablecer_password?token={token}"
        
        # 🔥 LOGS DE DEBUG
        print(f"📧 Intentando enviar email a: {email}")
        print(f"🔗 URL de recuperación: {reset_url}")
        print(f"🏢 Cliente ID: {cliente_id}")
        
        try:
            # Enviar email con manejo de errores
            resultado = enviar_email_recuperacion(email, reset_url)
            if resultado:
                print("✅ Email enviado exitosamente")
            else:
                print("⚠️ Email no se pudo enviar, pero continuamos")
        except Exception as email_error:
            print(f"❌ Error al enviar email: {str(email_error)}")
            # No detenemos el flujo, solo registramos el error
        
        return jsonify({"mensaje": "Si el email existe, recibirás instrucciones"}), 200
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error en recuperar_password: {str(e)}")
        import traceback
        traceback.print_exc()
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
        
        # Para la recuperación, necesitamos encontrar el cliente_id del token
        conn = conectar_db()
        if not conn:
            return "Error de conexión", 500
            
        try:
            cur = conn.cursor()
            # Buscar el token en cualquier cliente (no solo en el subdominio actual)
            cur.execute("""
                SELECT id, cliente_id FROM users 
                WHERE reset_token = %s AND reset_expiracion > NOW()
            """, (token,))
            
            result = cur.fetchone()
            if not result:
                return "Token inválido o expirado", 400
                
            user_id, cliente_id = result
            
            # Guardar el cliente_id en la sesión para el POST
            session['reset_cliente_id'] = cliente_id
            session['reset_token'] = token
            
            return render_template_string("""...""", token=token)
            
        finally:
            liberar_db(conn)
    
    # POST: actualizar contraseña
    datos = request.json
    token = datos.get("token")
    password = datos.get("password")
    
    if not token or not password or len(password) < 6:
        return jsonify({"error": "Datos inválidos"}), 400
        
    # Usar el cliente_id guardado en la sesión
    cliente_id = session.get('reset_cliente_id')
    if not cliente_id:
        return jsonify({"error": "Sesión inválida"}), 400
        
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "Error de conexión"}), 500
        
    try:
        cur = conn.cursor()
        nuevo_hash = generate_password_hash(password)
        cur.execute("""
            UPDATE users 
            SET password_hash = %s, reset_token = NULL, reset_expiracion = NULL
            WHERE reset_token = %s AND cliente_id = %s
        """, (nuevo_hash, token, cliente_id))
        
        if cur.rowcount == 0:
            return jsonify({"error": "Token inválido o expirado"}), 400
            
        conn.commit()
        # Limpiar sesión
        session.pop('reset_cliente_id', None)
        session.pop('reset_token', None)
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
    
    
