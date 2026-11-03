"""
Azure Function: alertas de WhatsApp (Twilio) para la planta solar de Sebastian Caujaral
a partir de GoodWe SEMS+.

- Revisa cada 5 minutos (timer trigger).
- Guarda el último estado en Azure Blob Storage (la misma cuenta de la Function App).
- Si algo falla varias veces seguidas, avisa al administrador por WhatsApp,
  y avisa otra vez cuando se recupera.
- Todos los días a las 7:00 a. m. (Bogotá) manda al administrador un aviso de "sistema activo".

Configuración (Variables de entorno de la Function App):
  SEMS_USER, SEMS_PASSWORD, SEMS_STATION_ID
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM
  CLIENTE_WHATSAPP_TO   -> a quién le llegan las alertas de la planta
  ADMIN_WHATSAPP_TO     -> a quién le llegan los avisos de falla / sistema activo
"""

import logging

import azure.functions as func

import alertas

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False,
                   use_monitor=True)
def revisar_planta(timer: func.TimerRequest) -> None:
    alertas.ciclo()


@app.timer_trigger(schedule="0 0 12 * * *", arg_name="timer", run_on_startup=False,
                   use_monitor=True)
def latido_diario(timer: func.TimerRequest) -> None:
    # 12:00 UTC = 7:00 a. m. Bogotá
    alertas.latido()


@app.route(route="probar", auth_level=func.AuthLevel.FUNCTION, methods=["GET", "POST"])
def probar(req: func.HttpRequest) -> func.HttpResponse:
    """Envía un WhatsApp de prueba al cliente y devuelve la lectura actual."""
    try:
        texto = alertas.prueba()
        return func.HttpResponse(texto, status_code=200)
    except Exception as e:  # noqa: BLE001
        logging.exception("Prueba falló")
        return func.HttpResponse(f"Error: {e}", status_code=500)
