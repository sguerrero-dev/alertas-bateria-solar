"""
Alertas de WhatsApp (Twilio) a partir de los datos de GoodWe SEMS+.
Cliente: Sebastián Caujaral

Fuente de datos: SEMS+ (semsplus.goodwe.com), endpoint del flujo de energía
de la planta (la misma gráfica Solar / Batería / Red / Carga del panel).

Reglas (en orden de prioridad):
  3) Solar y batería desconectados (0 kW)  -> "Hay luz en Caujaral"
  4) Solar desconectado y batería <= 50%   -> "Caujaral está sin luz, raciona tu uso, batería al 50%"
  2) Batería <= 50%                         -> "Tu batería está al 50%, raciona energía"
  1) Batería <= 94%                         -> "Caujaral se quedó sin luz, raciona el uso..."

Solo se envía un mensaje cuando el estado CAMBIA (no repite la misma alerta).

Uso:
  python goodwe_whatsapp_alertas.py            # corre en bucle cada INTERVALO_MIN minutos
  python goodwe_whatsapp_alertas.py --once     # una sola revisión (para cron / Programador de tareas)
  python goodwe_whatsapp_alertas.py --debug    # muestra los datos reales de la planta y qué regla aplica
  python goodwe_whatsapp_alertas.py --test     # envía un WhatsApp de prueba

Login y firma de SEMS+ basados en la integración open source (MIT)
github.com/TimSoethout/goodwe-sems-home-assistant
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

# ---------------------------------------------------------------- Configuración
SEMS_USER = os.getenv("SEMS_USER")
SEMS_PASSWORD = os.getenv("SEMS_PASSWORD")
SEMS_STATION_ID = os.getenv("SEMS_STATION_ID") or "15b1e11c-b2b4-4d81-9fdc-b08559527511"

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or "whatsapp:+14155238886"  # sandbox por defecto
CLIENTE_TO = os.getenv("CLIENTE_WHATSAPP_TO")  # ej: whatsapp:+573001234567

INTERVALO_MIN = int(os.getenv("INTERVALO_MIN", "5"))
UMBRAL_SIN_LUZ = float(os.getenv("UMBRAL_SIN_LUZ", "94"))
UMBRAL_BAJO = float(os.getenv("UMBRAL_BAJO", "50"))
KW_MINIMOS = float(os.getenv("KW_MINIMOS", "0.02"))  # por debajo de esto = "desconectado"
STATE_FILE = Path(os.getenv("STATE_FILE", "estado_alertas.json"))

LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
API_FALLBACK = "https://us-gateway.semsportal.com/web/sems"
FLOW_PATH = "/sems-plant/api/stations/flow"
INFO_PATH = "/sems-plant/api/portal/stations/basic/info"
OK_CODES = {0, "0", "00000"}
TOKEN_EXPIRADO = {"100002", "C0602"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MENSAJES = {
    "item1": "⚠️ Caujaral se quedó sin luz. Raciona el uso de tu batería, tu batería está cargada.",
    "item2": "🔋 Tu batería está al 50%. Raciona energía.",
    "item3": "💡 Hay luz en Caujaral.",
    "item4": "⚠️ Caujaral está sin luz. Raciona tu uso, batería al 50%.",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("caujaral")


# ---------------------------------------------------------------- GoodWe SEMS+
def _firma(token_data):
    """X-Signature que exige SEMS+ Web: base64(sha256(ms@uid@token)@ms)."""
    ms = round(time.time() * 1000)
    digest = hashlib.sha256(
        f"{ms}@{token_data.get('uid', '')}@{token_data.get('token', '')}".encode()
    ).hexdigest()
    return base64.b64encode(f"{digest}@{ms}".encode()).decode()


class SemsPlus:
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.token = None

    def login(self):
        pwd = base64.b64encode(hashlib.md5(self.password.encode()).hexdigest().encode()).decode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://semsplus.goodwe.com",
            "Referer": "https://semsplus.goodwe.com/",
            "User-Agent": UA,
            "Token": json.dumps({"uid": "", "timestamp": 0, "token": "", "client": "semsPlusWeb",
                                 "version": "", "language": "es"}),
            "X-Signature": _firma({}),
        }
        body = {"account": self.user, "pwd": pwd, "agreement": 1,
                "isChinese": False, "isLocal": False}
        r = requests.post(LOGIN_URL, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        resp = r.json()
        if resp.get("code") not in OK_CODES or not isinstance(resp.get("data"), dict):
            raise RuntimeError(f"Login SEMS+ falló (código {resp.get('code')}): "
                               f"{resp.get('description') or resp.get('msg')}")
        token = dict(resp["data"])
        token["api"] = resp.get("api") or token.get("api") or API_FALLBACK
        self.token = token
        log.info("Login SEMS+ OK (%s)", token["api"])

    def _get(self, path, reintento=True):
        if not self.token:
            self.login()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
            "token": json.dumps(self.token),
            "X-Signature": _firma(self.token),
        }
        r = requests.get(self.token["api"].rstrip("/") + path, headers=headers, timeout=30)
        if r.status_code == 401 and reintento:
            self.token = None
            return self._get(path, reintento=False)
        r.raise_for_status()
        resp = r.json()
        if resp.get("code") not in OK_CODES:
            if str(resp.get("code")) in TOKEN_EXPIRADO and reintento:
                self.token = None
                return self._get(path, reintento=False)
            raise RuntimeError(f"SEMS+ devolvió error {resp.get('code')}: {resp.get('description')}")
        return resp.get("data") or {}

    def flujo(self, station_id):
        return self._get(f"{FLOW_PATH}?stationId={station_id}")

    def info(self, station_id):
        return self._get(f"{INFO_PATH}?stationId={station_id}")


# ---------------------------------------------------------------- Lectura y reglas
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extraer_lectura(flow):
    """Del JSON de /stations/flow -> {soc %, solar_kw, bateria_kw, red_kw}."""
    solar = _f(flow.get("pSystem"))
    if solar is None:
        solar = _f(flow.get("pAc"))
    return {
        "soc": _f(flow.get("soc")),
        "solar_kw": solar or 0.0,
        "bateria_kw": _f(flow.get("pBat")) or 0.0,  # >0 descargando, <0 cargando
        "red_kw": _f(flow.get("pGrid")) or 0.0,
        "hora": flow.get("refreshTime"),
    }


def evaluar(l):
    soc = l["soc"]
    solar_off = abs(l["solar_kw"]) < KW_MINIMOS
    bateria_off = abs(l["bateria_kw"]) < KW_MINIMOS

    if solar_off and bateria_off:
        return "item3"
    if soc is None:
        return None
    if solar_off and soc <= UMBRAL_BAJO:
        return "item4"
    if soc <= UMBRAL_BAJO:
        return "item2"
    if soc <= UMBRAL_SIN_LUZ:
        return "item1"
    return "normal"  # batería > 94%: sin alerta


# ---------------------------------------------------------------- Estado y envío
def leer_estado():
    try:
        return json.loads(STATE_FILE.read_text()).get("ultimo")
    except Exception:
        return None


def guardar_estado(estado):
    STATE_FILE.write_text(json.dumps({"ultimo": estado, "ts": time.time()}))


def enviar_whatsapp(texto):
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(from_=TWILIO_FROM, to=CLIENTE_TO, body=texto)
    log.info("WhatsApp enviado (sid %s): %s", msg.sid, texto)


def revisar(sems):
    lectura = extraer_lectura(sems.flujo(SEMS_STATION_ID))
    estado = evaluar(lectura)
    anterior = leer_estado()
    log.info("Lectura %s -> estado %s (anterior %s)", lectura, estado, anterior)

    if estado and estado != anterior:
        if estado in MENSAJES and not (estado == "item3" and anterior is None):
            # (la primera vez no avisa "hay luz", solo guarda el estado)
            enviar_whatsapp(MENSAJES[estado])
        guardar_estado(estado)


def validar_config(necesita_twilio=True, necesita_sems=True):
    req = {}
    if necesita_sems:
        req.update({"SEMS_USER": SEMS_USER, "SEMS_PASSWORD": SEMS_PASSWORD})
    if necesita_twilio:
        req.update({"TWILIO_ACCOUNT_SID": TWILIO_SID, "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
                    "CLIENTE_WHATSAPP_TO": CLIENTE_TO})
    faltan = [k for k, v in req.items() if not v]
    if faltan:
        sys.exit(f"Faltan variables en .env: {', '.join(faltan)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()

    if args.test:
        validar_config(necesita_sems=False)
        enviar_whatsapp("✅ Prueba de alertas Caujaral: conexión Twilio OK.")
        return

    if args.debug:
        validar_config(necesita_twilio=False)
        sems = SemsPlus(SEMS_USER, SEMS_PASSWORD)
        info = sems.info(SEMS_STATION_ID)
        flow = sems.flujo(SEMS_STATION_ID)
        print("Planta:", info.get("name"), "| estado:", info.get("status"),
              "| gridStatus:", info.get("gridStatus"))
        print(json.dumps(flow, indent=2, ensure_ascii=False))
        l = extraer_lectura(flow)
        print("Lectura:", l, "->", evaluar(l))
        return

    validar_config()
    sems = SemsPlus(SEMS_USER, SEMS_PASSWORD)

    if args.once:
        revisar(sems)
        return

    log.info("Monitoreando planta %s cada %s min...", SEMS_STATION_ID, INTERVALO_MIN)
    while True:
        try:
            revisar(sems)
        except Exception as e:
            log.error("Error en la revisión: %s", e)
        time.sleep(INTERVALO_MIN * 60)


if __name__ == "__main__":
    main()
