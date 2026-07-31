"""KD Radar — Paso 2: Extraer emails de las webs de los restaurantes.

Estrategia (en orden de prioridad):
  1. Aviso legal / política de privacidad -> suele contener el email oficial
     de la empresa (obligatorio por LSSI Art. 10).
  2. Página de contacto.
  3. Portada (mailto:, texto plano, ofuscación Cloudflare).

Solo se guardan buzones corporativos/genéricos cuando hay varios candidatos
(info@, reservas@...), para minimizar tratamiento de datos de personas físicas.

Uso:
    python 2_extraer_emails.py
"""
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import PREFIJOS_GENERICOS, DOMINIOS_BASURA, EXTENSIONES_FALSAS
from db import init_db, leads_por_estado, actualizar_lead, stats

RUTAS_CANDIDATAS = [
    "",  # portada
    "aviso-legal", "avisolegal", "aviso_legal", "legal",
    "politica-de-privacidad", "politica-privacidad", "privacidad", "privacy",
    "contacto", "contact", "contactanos", "contacta",
]

REGEX_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def decodificar_cfemail(cf: str) -> str:
    """Decodifica emails ofuscados por Cloudflare (data-cfemail, XOR simple)."""
    try:
        clave = int(cf[:2], 16)
        return "".join(chr(int(cf[i:i + 2], 16) ^ clave)
                       for i in range(2, len(cf), 2))
    except (ValueError, IndexError):
        return ""


def es_email_valido(email: str) -> bool:
    e = email.lower()
    if e.endswith(EXTENSIONES_FALSAS):
        return False
    dominio = e.split("@")[-1]
    if any(basura in dominio for basura in DOMINIOS_BASURA):
        return False
    if len(e) > 80 or e.count("@") != 1:
        return False
    return True


def extraer_de_html(html: str) -> set[str]:
    encontrados: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")

    # 1) mailto:
    for a in soup.select('a[href^="mailto:"]'):
        email = a["href"].removeprefix("mailto:").split("?")[0].strip()
        if email:
            encontrados.add(email)

    # 2) Ofuscación Cloudflare
    for tag in soup.select("[data-cfemail]"):
        email = decodificar_cfemail(tag["data-cfemail"])
        if email:
            encontrados.add(email)

    # 3) Texto plano (incluye avisos legales)
    encontrados.update(REGEX_EMAIL.findall(soup.get_text(" ")))
    # También en el HTML crudo (emails en atributos o JSON embebido)
    encontrados.update(REGEX_EMAIL.findall(html))

    return {e.strip(".,;:").lower() for e in encontrados if es_email_valido(e)}


def elegir_mejor(emails: set[str], dominio_web: str) -> str | None:
    """Prioriza: prefijo genérico + dominio propio > genérico > dominio propio > resto."""
    if not emails:
        return None

    def puntuar(e: str) -> tuple:
        prefijo = e.split("@")[0]
        dominio = e.split("@")[-1]
        es_generico = any(prefijo == p or prefijo.startswith(p)
                          for p in PREFIJOS_GENERICOS)
        es_propio = dominio_web and dominio_web in dominio
        # Menor tupla = mejor
        return (not (es_generico and es_propio), not es_generico,
                not es_propio, len(e))

    return sorted(emails, key=puntuar)[0]


def procesar_lead(lead: dict, cliente: httpx.Client) -> tuple[str | None, str | None]:
    base = lead["web"]
    parsed = urlparse(base)
    if not parsed.scheme:
        base = "https://" + base
        parsed = urlparse(base)
    dominio_web = parsed.netloc.removeprefix("www.")

    for ruta in RUTAS_CANDIDATAS:
        url = urljoin(base if base.endswith("/") else base + "/", ruta)
        try:
            r = cliente.get(url, headers={"User-Agent": UA})
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            emails = extraer_de_html(r.text)
            mejor = elegir_mejor(emails, dominio_web)
            if mejor:
                return mejor, str(r.url)
        except httpx.HTTPError:
            continue
        time.sleep(0.3)
    return None, None


def main():
    init_db()
    pendientes = leads_por_estado("nuevo", con_web=True)
    print(f"Leads con web pendientes de extracción: {len(pendientes)}")

    # Marcar sin_web los que no tienen web (candidatos a llamada de Toni)
    for lead in leads_por_estado("nuevo"):
        if not lead["web"]:
            actualizar_lead(lead["id"], estado="sin_web")

    con_email = 0
    with httpx.Client(timeout=15, follow_redirects=True) as cliente:
        for i, lead in enumerate(pendientes, 1):
            email, fuente = procesar_lead(lead, cliente)
            if email:
                actualizar_lead(lead["id"], email=email, email_fuente=fuente,
                                estado="con_email")
                con_email += 1
                print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} -> {email}")
            else:
                actualizar_lead(lead["id"], estado="sin_email")
                print(f"[{i}/{len(pendientes)}] {lead['nombre'][:40]:40} -> (sin email)")

    print(f"\nEmails encontrados: {con_email}/{len(pendientes)}")
    print("Resumen:", stats())


if __name__ == "__main__":
    main()
