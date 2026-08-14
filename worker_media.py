import logging
import os
import signal
import threading
import time

from app import (
    conectar_db,
    liberar_db,
    procesar_un_trabajo_multimedia,
    recuperar_leases_multimedia_vencidos,
)


LOGGER = logging.getLogger("crm_media_worker")
STOP_REQUESTED = threading.Event()


def _obtener_segundos_positivos(nombre, valor_default):
    valor = os.getenv(nombre)
    if valor is None or not valor.strip():
        return float(valor_default)
    try:
        segundos = float(valor)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Configuración inválida: {nombre}") from exc
    if segundos <= 0:
        raise RuntimeError(f"Configuración inválida: {nombre}")
    return segundos


def _validar_configuracion_critica():
    variables_requeridas = (
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "S3_BUCKET_NAME",
    )
    faltantes = [nombre for nombre in variables_requeridas if not os.getenv(nombre)]
    if faltantes:
        raise RuntimeError(
            "Configuración multimedia incompleta: " + ", ".join(faltantes)
        )

    conn = conectar_db()
    if not conn:
        raise RuntimeError("Base de datos no disponible al iniciar el worker")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    finally:
        conn.rollback()
        liberar_db(conn)


def _solicitar_detencion(signum, _frame):
    LOGGER.info("Señal de detención recibida: signal=%s", signum)
    STOP_REQUESTED.set()


def ejecutar_worker():
    idle_seconds = _obtener_segundos_positivos(
        "WHATSAPP_MEDIA_WORKER_IDLE_SECONDS",
        2,
    )
    recovery_interval = _obtener_segundos_positivos(
        "WHATSAPP_MEDIA_LEASE_RECOVERY_INTERVAL_SECONDS",
        60,
    )
    _validar_configuracion_critica()

    signal.signal(signal.SIGTERM, _solicitar_detencion)
    signal.signal(signal.SIGINT, _solicitar_detencion)

    recuperados = recuperar_leases_multimedia_vencidos()
    if recuperados:
        LOGGER.warning("Leases multimedia recuperados al iniciar: count=%s", recuperados)

    proxima_recuperacion = time.monotonic() + recovery_interval
    LOGGER.info("Media worker iniciado")

    try:
        while not STOP_REQUESTED.is_set():
            ahora = time.monotonic()
            if ahora >= proxima_recuperacion:
                try:
                    recuperados = recuperar_leases_multimedia_vencidos()
                    if recuperados:
                        LOGGER.warning(
                            "Leases multimedia recuperados: count=%s",
                            recuperados,
                        )
                except Exception as exc:
                    LOGGER.error(
                        "Error en lease recovery: tipo_error=%s",
                        type(exc).__name__,
                    )
                finally:
                    proxima_recuperacion = time.monotonic() + recovery_interval

            try:
                resultado = procesar_un_trabajo_multimedia()
            except Exception as exc:
                LOGGER.error(
                    "Error inesperado en el loop multimedia: tipo_error=%s",
                    type(exc).__name__,
                )
                STOP_REQUESTED.wait(idle_seconds)
                continue

            status = resultado.get("status") if isinstance(resultado, dict) else None
            if status == "no_job":
                STOP_REQUESTED.wait(idle_seconds)
            elif status not in {"completed", "failed"}:
                LOGGER.warning("Resultado multimedia inesperado: status=%s", status)
                STOP_REQUESTED.wait(idle_seconds)
    finally:
        LOGGER.info("Media worker detenido")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ejecutar_worker()
