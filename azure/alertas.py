"""Lógica de alertas: SEMS+ -> reglas -> Twilio WhatsApp, con estado en Blob Storage."""

import base64
import hashlib
import json
import logging
import os
import time

import requests

log = logging.getLogger("caujaral")

# ---------------------------------------------------------------- Configuración
def cfg(nombre, defecto=None):
    v = os.getenv(nombre)
    return v if v not in (None, "") else defecto


UMBRAL_SIN_LUZ = float(cfg("UMBRAL_SIN_LUZ", "94"))
UMBRAL_BAJO = float(cfg("UMBRAL_BAJO", "50"))
KW_MINIMOS = float(cfg("KW_MINIMOS", "0.02"))
FALLAS_PARA_AVISAR = int(cfg("FALLAS_PARA_AVISAR", "6"))  # 6 x 5 min = 30 min

LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
API_FALLBACK = "https://us-gateway.semsportal.com/web/sems"
FLOW_PATH = "/sems-plant/api/stations/flow"
OK_CODES = {0, "0", "00000"}
TOKEN_EXPIRADO = {"100002", "C0602"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ENCABEZADO = "*⚡ Salva Energy*\n"
MENSAJES = {
    "item1": "⚠️ Caujaral se quedó sin luz, raciona el uso de tu batería, tu batería está cargada",
    "item2": "🔋 Tu batería está al 50%, raciona energía",
    "item3": "💡 Hay luz en Caujaral",
    "item4": "⚠️ Caujaral está sin luz, raciona tu uso, batería al 50%",
}

# ---------------------------------------------------------------- Estado (Blob)
_CONTENEDOR = "alertas-caujaral"
_BLOB = "estado.json"


def _blob():
    from azure.storage.blob import BlobServiceClient
    conn = cfg("AzureWebJobsStorage")
    if conn:
        svc = BlobServiceClient.from_connection_string(conn)
    else:  # Flex Consumption con identidad administrada
        from azure.identity import DefaultAzureCredential
        cuenta = cfg("AzureWebJobsStorage__accountName")
        svc = BlobServiceClient(f"https://{cuenta}.blob.core.windows.net",
                                credential=DefaultAzureCredential())
    cont = svc.get_container_client(_CONTENEDOR)
    try:
        cont.create_container()
    except Exception:  # ya existe
        pass
    return cont.get_blob_client(_BLOB)


def leer_estado():
    try:
        return json.loads(_blob().download_blob().readall())
    except Exception:
        return {}


def guardar_estado(estado):
    _blob().upload_blob(json.dumps(estado), overwrite=True)


# ---------------------------------------------------------------- SEMS+
def _firma(token_data):
    ms = round(time.time() * 1000)
    digest = hashlib.sha256(
        f"{ms}@{token_data.get('uid', '')}@{token_data.get('token', '')}".encode()
    ).hexdigest()
    return base64.b64encode(f"{digest}@{ms}".encode()).decode()


def login():
    pwd = base64.b64encode(hashlib.md5(cfg("SEMS_PASSWORD", "").encode()).hexdigest().encode()).decode()
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
    body = {"account": cfg("SEMS_USER"), "pwd": pwd, "agreement": 1,
            "isChinese": False, "isLocal": False}
    r = requests.post(LOGIN_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") not in OK_CODES or not isinstance(resp.get("data"), dict):
        raise RuntimeError(f"Login SEMS+ falló (código {resp.get('code')}): "
                           f"{resp.get('description') or resp.get('msg')}")
    token = dict(resp["data"])
    token["api"] = resp.get("api") or token.get("api") or API_FALLBACK
    return token


def leer_flujo(token):
    station = cfg("SEMS_STATION_ID", "15b1e11c-b2b4-4d81-9fdc-b08559527511")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": UA, "token": json.dumps(token), "X-Signature": _firma(token)}
    r = requests.get(f"{token['api'].rstrip('/')}{FLOW_PATH}?stationId={station}",
                     headers=headers, timeout=30)
    if r.status_code == 401:
        raise PermissionError("token vencido")
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") not in OK_CODES:
        if str(resp.get("code")) in TOKEN_EXPIRADO:
            raise PermissionError("token vencido")
        raise RuntimeError(f"SEMS+ devolvió error {resp.get('code')}: {resp.get('description')}")
    return resp.get("data") or {}


def obtener_lectura(estado):
    """Reutiliza el token guardado; si vence, inicia sesión de nuevo."""
    token = estado.get("token")
    for intento in range(2):
        if not token:
            token = login()
        try:
            flow = leer_flujo(token)
            estado["token"] = token
            return extraer_lectura(flow)
        except PermissionError:
            token = None
    raise RuntimeError("No se pudo leer SEMS+ después de reiniciar sesión")


# ---------------------------------------------------------------- Reglas
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extraer_lectura(flow):
    solar = _f(flow.get("pSystem"))
    if solar is None:
        solar = _f(flow.get("pAc"))
    return {"soc": _f(flow.get("soc")), "solar_kw": solar or 0.0,
            "bateria_kw": _f(flow.get("pBat")) or 0.0, "hora": flow.get("refreshTime")}


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
    return "normal"


# ---------------------------------------------------------------- Twilio
def enviar_whatsapp(texto, destino=None):
    destino = destino or cfg("CLIENTE_WHATSAPP_TO")
    sid, tok = cfg("TWILIO_ACCOUNT_SID"), cfg("TWILIO_AUTH_TOKEN")
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, tok), timeout=30,
        data={"From": cfg("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
              "To": destino, "Body": ENCABEZADO + texto})
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio respondió {r.status_code}: {r.text[:300]}")
    log.info("WhatsApp enviado a %s: %s", destino, texto)


def avisar_admin(texto):
    admin = cfg("ADMIN_WHATSAPP_TO")
    if not admin:
        return
    try:
        enviar_whatsapp(texto, admin)
    except Exception:
        log.exception("No se pudo avisar al administrador")


# ---------------------------------------------------------------- Ciclos
def ciclo():
    estado = leer_estado()
    try:
        lectura = obtener_lectura(estado)
        nuevo = evaluar(lectura)
        anterior = estado.get("ultimo")
        log.info("Lectura %s -> %s (anterior %s)", lectura, nuevo, anterior)

        if nuevo and nuevo != anterior:
            if nuevo in MENSAJES and not (nuevo == "item3" and anterior is None):
                enviar_whatsapp(MENSAJES[nuevo])
            estado["ultimo"] = nuevo

        if estado.get("fallas", 0) >= FALLAS_PARA_AVISAR:
            avisar_admin("✅ Alertas Caujaral: el sistema se recuperó y volvió a leer la planta.")
        estado["fallas"] = 0
        estado["ultima_lectura"] = lectura
        estado["ultima_ok"] = time.time()
    except Exception as e:  # noqa: BLE001
        estado["fallas"] = estado.get("fallas", 0) + 1
        estado["token"] = None
        log.exception("Falla %s en la revisión", estado["fallas"])
        if estado["fallas"] == FALLAS_PARA_AVISAR:
            avisar_admin(f"🚨 Alertas Caujaral: no he podido revisar la planta en los "
                         f"últimos {FALLAS_PARA_AVISAR * 5} minutos.\nError: {str(e)[:200]}")
    finally:
        guardar_estado(estado)


def latido():
    estado = leer_estado()
    l = estado.get("ultima_lectura") or {}
    hace = (time.time() - estado.get("ultima_ok", 0)) / 60
    avisar_admin(f"🟢 Alertas Caujaral activo.\nÚltima lectura hace {hace:.0f} min: "
                 f"batería {l.get('soc')}%, solar {l.get('solar_kw')} kW, "
                 f"estado {estado.get('ultimo')}.")


def prueba():
    estado = leer_estado()
    lectura = obtener_lectura(estado)
    guardar_estado(estado)
    enviar_whatsapp("✅ Prueba de alertas Caujaral desde Azure: conexión OK.")
    return f"OK. Lectura actual: {lectura} -> {evaluar(lectura)}"
