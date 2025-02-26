from servicios import SERVICIOS 

FLUJO_CONVERSACION = {
    "inicio": {
        "respuestas": {
            "hola": {
                "mensaje": "Hola, a tus órdenes. ¿En qué puedo ayudarte?",
                "siguiente_estado": "servicios"
            }
        }
    },
    "servicios": {
        "respuestas": {
            "me puedes dar info": {
                "mensaje": "Por supuesto, los servicios que manejamos en Camicam Photobooth son: (Enviar imagen con los servicios detallados). ¿Qué tipo de evento tienes?",
                "siguiente_estado": "tipo_evento"
            }
        }
    },
    "tipo_evento": {
        "respuestas": {
            "xv años": {
                "mensaje": "¡Excelente! Puedes armar tu paquete con lo que necesites o si prefieres puedes ver el 'Paquete Mis XV' que hemos diseñado especialmente para ti. ¿Te interesa?",
                "siguiente_estado": "paquete_xv"
            }
        }
    },
    "paquete_xv": {
        "respuestas": {
            "qué incluye el paquete": {
                "mensaje": "(Enviar los datos del paquete MIS XV). ¿Te interesa o prefieres armar tu paquete personalizado?",
                "siguiente_estado": "armar_paquete"
            }
        }
    },
    "armar_paquete": {
        "respuestas": {
            "quiero armar mi paquete": {
                "mensaje": "¡Genial! Por favor, dime qué servicios ocupas.",
                "siguiente_estado": "servicios_solicitados"
            }
        }
    },
    "servicios_solicitados": {
        "respuestas": {
            "cabina de fotos y letras": {
                "mensaje": "Muy bien. Necesitas la cabina de fotos y 5 letras gigantes iluminadas. Aquí tienes una cotización: (Enviar video de cabina de fotos y su información). (Enviar video de letras gigantes y su información). ¿Puedes decirme la fecha de tu evento para revisar disponibilidad?",
                "siguiente_estado": "fecha_evento"
            }
        }
    },
    "fecha_evento": {
        "respuestas": {
            "fecha": {
                "mensaje": "Revisando disponibilidad... (Respuesta basada en disponibilidad).",
                "siguiente_estado": "seguimiento"
            }
        }
    },
    "seguimiento": {
        "respuestas": {
            "muchas gracias": {
                "mensaje": "Claro, cualquier duda con toda confianza. Si deseas agregar más servicios a tu cotización, se puede mejorar el precio. Si requieres cualquier otra cotización, estoy para servirte.",
                "siguiente_estado": "final"
            }
        }
    },
    "final": {
        "respuestas": {
            "muchas gracias": {
                "mensaje": "Que tengas un excelente día.",
                "siguiente_estado": None
            }
        }
    }
}