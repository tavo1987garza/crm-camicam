

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
import re
import time 
import base64
import uuid
from datetime import datetime, timezone, date 
from flask import send_from_directory
import json
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
        # 1) Parámetro límite
        lim = int(request.args.get("limite", 5))

        # 2) Consulta
        conn = conectar_db()
        cur  = conn.cursor()
        cur.execute("""
          SELECT 
            id,
            TO_CHAR(fecha AT TIME ZONE 'UTC','YYYY-MM-DD') AS fecha,
            COALESCE(titulo,'') AS titulo,
            COALESCE(servicios::text, '{}')   AS servicios_text
          FROM calendario
          WHERE fecha AT TIME ZONE 'UTC' >= %s
          ORDER BY fecha ASC
          LIMIT %s
        """, (date.today(), lim))

        rows = cur.fetchall()
        liberar_db(conn)

        # 3) Construcción segura de JSON
        eventos = []
        for r in rows:
            raw = r[3]
            try:
                servicios = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as e:
                app.logger.error(f"Error parseando servicios JSON (‘{raw}’): {e}")
                servicios = {}

            eventos.append({
                "id":        r[0],
                "fecha":     r[1],
                "titulo":    r[2],
                "servicios": servicios
            })

        return jsonify(eventos), 200

    except Exception as exc:
        # Log completo de la excepción
        app.logger.exception("500 en /calendario/proximos")
        return jsonify({"error":"Ocurrió un error al obtener próximos eventos"}), 500




# 📌 Endpoint para mostras los Ultimos Leads
@app.route("/leads/ultimos")
def ultimos_leads():
    lim = int(request.args.get("limite", 3))
    conn = conectar_db(); cur = conn.cursor()
    cur.execute("""
      SELECT id, nombre, telefono
      FROM leads
      ORDER BY id DESC
      LIMIT %s
    """, (lim,))
    rows = cur.fetchall()
    liberar_db(conn)
    return jsonify([{"id":r[0],"nombre":r[1],"telefono":r[2]} for r in rows])


# 📌 Endpoint para mostrar el FPI ensual (La meta mensual)
@app.route("/reportes/kpi_mes")
def kpi_mes():
    hoy = datetime.utcnow()
    mes = hoy.month
    anio = hoy.year

    conn = conectar_db(); cur = conn.cursor()
    # actual: count eventos en el mes actual
    cur.execute("""
      SELECT COUNT(*) 
      FROM calendario 
      WHERE EXTRACT(YEAR FROM fecha AT TIME ZONE 'UTC')=%s
        AND EXTRACT(MONTH FROM fecha AT TIME ZONE 'UTC')=%s
    """, (anio, mes))
    actual = cur.fetchone()[0]

    # meta: lee de tabla config
    cur.execute("""
      SELECT valor 
      FROM config 
      WHERE clave='meta_mensual' 
    """)
    row = cur.fetchone()
    meta = int(row[0]) if row and row[0].isdigit() else 15

    liberar_db(conn)
    return jsonify({"actual": actual, "meta": meta})



##################################
#----------SECCION LEADS----------
##################################   

# 📌 Endpoint para recibir mensajes desde WhatsApp
@app.route("/recibir_mensaje", methods=["POST"])
def recibir_mensaje():
    datos = request.json
    plataforma = datos.get("plataforma")
    # Convertir remitente a string para poder hacer slicing o concatenar
    remitente = str(datos.get("remitente", ""))
    mensaje = datos.get("mensaje")
    tipo = datos.get("tipo")  # podría ser "enviado", "recibido", "recibido_imagen", "enviado_imagen", etc.

    # ✅ Validaciones: asegurarse de tener plataforma, remitente y mensaje
    if not plataforma or not remitente or not mensaje:
        return jsonify({"error": "Faltan datos: plataforma, remitente o mensaje"}), 400

    # Permitir valores para mensajes de texto y de imagen
    if tipo not in ["enviado", "recibido", "recibido_imagen", "enviado_imagen"]:
        tipo = "recibido"

    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # 🔸 Verificar si existe un lead con ese teléfono
        cursor.execute("SELECT id, nombre FROM leads WHERE telefono = %s", (remitente,))
        lead = cursor.fetchone()   # lead será None si no hay fila, o una tupla (id, nombre)

        if not lead:
            # Si no hay lead, creamos uno con nombre por defecto
            nombre_por_defecto = f"Lead desde Chat {remitente[-10:]}"
            cursor.execute("""
                INSERT INTO leads (nombre, telefono, estado)
                VALUES (%s, %s, 'Contacto Inicial')
                ON CONFLICT (telefono) DO NOTHING
                RETURNING id
            """, (nombre_por_defecto, remitente))
            lead_id_row = cursor.fetchone()  # Esto será una tupla con el nuevo id (o None si no se insertó)
            if lead_id_row:
                lead_id = lead_id_row[0]  # Extraemos el entero id
            else:
                lead_id = None
        else:
            # Si sí existe, lead[0] es el id, lead[1] es el nombre
            lead_id = lead[0]
            nombre_por_defecto = None  # porque ya tenemos el lead existente

        # 🔸 Insertar el nuevo mensaje en la tabla `mensajes`
        cursor.execute("""
            INSERT INTO mensajes (plataforma, remitente, mensaje, estado, tipo)
            VALUES (%s, %s, %s, 'Nuevo', %s)
        """, (plataforma, remitente, mensaje, tipo))  # Aquí usamos el tipo correcto sin sobreescribir imágenes
        conn.commit()

        # 🔸 Emitir evento socket.io para el frontend
        socketio.emit("nuevo_mensaje", {
            "plataforma": plataforma,
            "remitente": remitente,
            "mensaje": mensaje,
            "tipo": tipo
        })

        # 🔸 Si se creó / existe un lead_id, emitimos 'nuevo_lead'
        if lead_id:
            if not lead:  # Si recién lo creamos, usamos nombre_por_defecto
                lead_nombre = nombre_por_defecto
            else:
                lead_nombre = lead[1]  # lead[1] es el nombre que ya estaba en la DB

            socketio.emit("nuevo_lead", {
                "id": lead_id,  # Usamos directamente lead_id (entero)
                "nombre": lead_nombre if lead_nombre else "",
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
CAMIBOT_API_URL = "https://cami-bot-7d4110f9197c.herokuapp.com"

@app.route("/enviar_mensaje", methods=["POST"])  
def enviar_mensaje():
    datos = request.json
    telefono = datos.get("telefono")
    tipo = datos.get("tipo", "texto")   # 'texto' por defecto
    url_imagen = datos.get("url")       # solo relevante si es imagen
    caption = datos.get("caption", "")
    mensaje_texto = datos.get("mensaje")

    if not telefono:
        return jsonify({"error": "Número de teléfono es obligatorio"}), 400

    # Enviamos la orden al bot:
    if tipo == "imagen":
        if not url_imagen:
            return jsonify({"error": "Falta la URL de la imagen"}), 400

        payload = {"telefono": telefono, "imageUrl": url_imagen, "caption": caption}
        max_intentos = 3
        for intento in range(max_intentos):
            try:
                respuesta = requests.post(f"{CAMIBOT_API_URL}/enviar_imagen",
                                          json=payload,
                                          timeout=5)
                if respuesta.status_code == 200:
                    break
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Intento {intento + 1} fallido: {str(e)}")
                time.sleep(2)
        return jsonify({"mensaje": "Imagen enviada correctamente"}), 200

    else:
        # Caso: Texto
        if not mensaje_texto:
            return jsonify({"error": "Falta el 'mensaje' de texto"}), 400

        payload = {"telefono": telefono, "mensaje": mensaje_texto}
        max_intentos = 3
        for intento in range(max_intentos):
            try:
                respuesta = requests.post(f"{CAMIBOT_API_URL}/enviar_mensaje",
                                          json=payload,
                                          timeout=5)
                if respuesta.status_code == 200:
                    break
            except requests.exceptions.RequestException as e:
                print(f"⚠️ Intento {intento + 1} fallido: {str(e)}")
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
            
          # 1️⃣  ¿Cuántos eventos hay ya ese día?
        cursor.execute("SELECT COUNT(*) FROM calendario WHERE fecha = %s", (fecha_str,))
        ya_hay = cursor.fetchone()[0]

        if ya_hay >= 2:
            return jsonify({"ok": False,
                            "mensaje": f"El {fecha_str} ya tiene 2 eventos registrados."}), 200

        if ya_hay == 1 and not force:
            # Hay 1 evento y aún no confirmas el segundo
            return jsonify({"ok": False,
                            "second_possible": True,
                            "mensaje": f"Ya hay un evento el {fecha_str}. ¿Agregar un segundo?"}), 200


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
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()

        # --- 1) Fechas con toda tu info ------------------------------
        cursor.execute("""
            SELECT 
                c.id,
                TO_CHAR(c.fecha AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS fecha,
                c.lead_id, 
                COALESCE(c.titulo, '')  AS titulo,
                COALESCE(c.notas, '')   AS notas,
                COALESCE(c.ticket, 0)   AS ticket,
                c.servicios,
                l.nombre               AS lead_nombre,
                EXTRACT(YEAR FROM c.fecha AT TIME ZONE 'UTC') AS anio
            FROM calendario c
            LEFT JOIN leads l ON c.lead_id = l.id
            ORDER BY c.fecha DESC
        """)
        fechas = []
        for row in cursor.fetchall():
            fechas.append({
                "id"        : row[0],
                "fecha"     : row[1],
                "lead_id"   : row[2],
                "titulo"    : row[3],
                "notas"     : row[4],
                "ticket"    : float(row[5]) if row[5] else 0.0,
                "servicios" : row[6] or {},
                "lead_nombre": row[7],
                "anio"      : int(row[8]) if row[8] else None
            })

        # --- 2) Colores definidos manualmente ------------------------
        cursor.execute("SELECT anio, color FROM anio_color")
        colores = {int(row[0]): row[1] for row in cursor.fetchall()}  # {2025:"#1e88e5", …}

        # --- 3) Respuesta -------------------------------------------
        return jsonify({
            "fechas" : fechas,
            "colores": colores           # 👈  nuevo campo
        }), 200

    except Exception as e:
        print(f"Error en fechas_ocupadas: {e}")
        return jsonify({"error": "Error al procesar fechas"}), 500
    finally:
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
    if "logo" not in request.files:
        return jsonify({"error":"Archivo faltante"}), 400

    file = request.files["logo"]
    if file.filename == "":
        return jsonify({"error":"Archivo inválido"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".png", ".jpg", ".jpeg", ".gif"]:
        return jsonify({"error":"Formato no soportado"}), 400

    filename = f"logo{ext}"
    path     = os.path.join("static", "logo", filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    file.save(path)

    # guarda URL en tabla config (clave 'logo_url')
    conn = conectar_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO config (clave, valor)
        VALUES ('logo_url', %s)
        ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor
    """, ("/static/logo/"+filename,))
    conn.commit()
    liberar_db(conn)

    return jsonify({"url": "/static/logo/"+filename}), 200


# RUTA PARA OBTENER LOGO
@app.route("/config/logo", methods=["GET"])
def obtener_logo():
    conn = conectar_db()
    cur  = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE clave='logo_url'")
    row = cur.fetchone()
    url = row[0] if row else "/static/logo/default.png"
    return jsonify({"url": url})






# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    
    
