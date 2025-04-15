

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
from flask import send_from_directory

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
    
    
    
    
# 📌 Endpoint para agregar fechas al Calendario    
@app.route("/calendario/agregar_manual", methods=["POST"])
def agregar_fecha_manual():
    data = request.json
    fecha_str = data.get("fecha")      # "YYYY-MM-DD"
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

        # Insertar en la tabla calendario
        cursor.execute("""
            INSERT INTO calendario (fecha, lead_id, titulo, notas, ticket, servicios)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (fecha) DO NOTHING
        """, (
            fecha_str,
            lead_id,
            titulo,
            notas,
            ticket_value,
            json.dumps(servicios_json)  # serializar dict a string JSON
        ))
        conn.commit()

        if cursor.rowcount == 0:
            # Significa que la fecha ya existía en la tabla (si UNIQUE(fecha))
            return jsonify({
                "ok": False,
                "mensaje": f"La fecha {fecha_str} ya está ocupada o existe en el calendario."
            }), 200

        return jsonify({
            "ok": True,
            "mensaje": f"Fecha {fecha_str} agregada correctamente al calendario."
        }), 200

    except Exception as e:
        print(f"❌ Error en /calendario/agregar_manual: {str(e)}")
        return jsonify({"error": str(e)}), 500

    finally:
        liberar_db(conn)

        
@app.route("/calendario/fechas_ocupadas", methods=["GET"])
def fechas_ocupadas():
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No hay DB"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.id, c.fecha, c.lead_id, 
                   COALESCE(c.titulo, '') as titulo,
                   COALESCE(c.notas, '') as notas,
                   l.nombre as lead_nombre
            FROM calendario c
            LEFT JOIN leads l ON c.lead_id = l.id
            ORDER BY c.fecha ASC
        """)
        rows = cursor.fetchall()
        
        # rows es una lista de tuplas, e.g. (1, datetime.date(2025,8,9), 3, "Boda", "notas...", "Daniel")
        # Conviértelo a objetos
        data = []
        for r in rows:
            fecha_str = r[1].strftime("%Y-%m-%d")  # conv. date -> "2025-08-09"
            data.append({
                "id": r[0],
                "fecha": fecha_str,
                "lead_id": r[2],
                "titulo": r[3],
                "notas": r[4],
                "lead_nombre": r[5]
            })

        return jsonify({"fechas": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
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
        
        # Consulta para obtener ingresos desglosados por mes
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM fecha) AS mes, COALESCE(SUM(ticket), 0) AS total_ingresos
            FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
            GROUP BY mes
            ORDER BY mes
        """, (anio,))
        rows_ing = cursor.fetchall()
        ingresos_por_mes = {mes: 0.0 for mes in range(1, 13)}
        for row in rows_ing:
            mes = int(row[0])
            total = float(row[1])
            ingresos_por_mes[mes] = total

        # Consulta para obtener gastos (costos) desglosados por mes
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM fecha) AS mes, COALESCE(SUM(monto), 0) AS total_gastos
            FROM gastos
            WHERE EXTRACT(YEAR FROM fecha) = %s
            GROUP BY mes
            ORDER BY mes
        """, (anio,))
        rows_gas = cursor.fetchall()
        gastos_por_mes = {mes: 0.0 for mes in range(1, 13)}
        for row in rows_gas:
            mes = int(row[0])
            total = float(row[1])
            gastos_por_mes[mes] = total

        # Consulta para contar el total de eventos registrados en el año
        cursor.execute("""
            SELECT COUNT(*)
            FROM calendario
            WHERE EXTRACT(YEAR FROM fecha) = %s
        """, (anio,))
        total_eventos = cursor.fetchone()[0] or 0

        return jsonify({
            "anio": int(anio),
            "ingresos_anual": ingresos_por_mes,
            "costos_anual": gastos_por_mes,
            "total_eventos": total_eventos
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

@app.route("/gastos/etiquetas", methods=["GET"])
def obtener_etiquetas():
    conn = conectar_db()
    if not conn:
        return jsonify({"error": "No se pudo conectar a la base de datos"}), 500

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT etiqueta FROM gasto_etiquetas ORDER BY etiqueta")
        rows = cursor.fetchall()
        etiquetas = [row[0] for row in rows]
        return jsonify({"etiquetas": etiquetas}), 200

    except Exception as e:
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
                "monto": float(row[0]),
                "descripcion": row[1],
                "fecha": row[4].strftime("%Y-%m-%d")  # o el formato que prefieras

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




# 📌 Endpoint para renderizar el Dashboard Web
@app.route("/dashboard")
def dashboard():
    return render_template("index.html")

# 📌 Iniciar la app con WebSockets
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    
    
