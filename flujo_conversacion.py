from servicios import SERVICIOS 

FLUJO_CONVERSACION = {
    "Contacto Inicial": {
        "respuestas": {
            "hola": {
                "mensaje": "Hola, a tus órdenes. ¿En qué puedo ayudarte?",
                "siguiente_estado": "En proceso"
            }
        }
    },
    "En proceso": {
        "respuestas": {
            "me puedes dar info": {
                "mensaje": "Por supuesto, los servicios que manejamos en Camicam Photobooth son: (Enviar imagen con los servicios detallados). ¿Qué tipo de evento tienes?",
                "siguiente_estado": "Seguimiento"
            }
        }
    },
    "Seguimiento": {
        "respuestas": {
            "quiero armar mi paquete": {
                "mensaje": "¡Genial! Por favor, dime qué servicios ocupas.",
                "siguiente_estado": "Seguimiento"
            }
        }
    },
    "Cliente": {
        "respuestas": {
            "muchas gracias": {
                "mensaje": "¡Gracias por elegirnos! Estamos aquí para lo que necesites.",
                "siguiente_estado": None
            }
        }
    },
    "No cliente": {
        "respuestas": {
            "adiós": {
                "mensaje": "Lamentamos no poder ayudarte. ¡Esperamos verte pronto!",
                "siguiente_estado": None
            }
        }
    }
}