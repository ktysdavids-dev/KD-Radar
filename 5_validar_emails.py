from __future__ import annotations
"""KD Radar — Paso 5: Validar emails (leads cualificados de verdad).

Tres niveles de validación por email:
  1. Sintaxis estricta (RFC simplificado).
  2. El dominio existe (DNS).
  3. El dominio acepta correo (registros MX).

Los emails que fallan se descartan y el lead pasa a 'sin_email'
(candidato a WhatsApp/visita, nunca a campaña de email).

Uso:
    python3 5_validar_emails.py
"""
import re
import time

import dns.resolver

from db import init_db, conexion, actualizar_lead, stats

REGEX_ESTRICTA = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._%+\-]{0,63}@"
    r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+$"
)

# Dominios desechables / typos frecuentes que invalidan el lead
DOMINIOS_INVALIDOS = {
    "gmail.co", "gmail.con", "gmial.com", "hotmail.co", "hotmail.con",
    "yahoo.co", "outlook.co", "mailinator.com", "tempmail.com",
    "guerrillamail.com", "10minutemail.com",
}

_cache_mx: dict[str, bool] = {}


def dominio_acepta_correo(dominio: str) -> bool:
    """True si el dominio tiene registros MX (o A como fallback RFC 5321)."""
    if dominio in _cache_mx:
        return _cache_mx[dominio]
    resultado = False
    try:
        respuesta = dns.resolver.resolve(dominio, "MX", lifetime=6)
        resultado = len(respuesta) > 0
    except (dns.resolver.NoAnswer,):
        # Sin MX explícito: fallback a registro A (válido según RFC)
        try:
            resultado = len(dns.resolver.resolve(dominio, "A", lifetime=6)) > 0
        except Exception:
            resultado = False
    except Exception:
        resultado = False
    _cache_mx[dominio] = resultado
    return resultado


def validar(email: str) -> tuple[bool, str]:
    email = (email or "").strip().lower()
    if not REGEX_ESTRICTA.match(email):
        return False, "sintaxis_invalida"
    dominio = email.split("@")[1]
    if dominio in DOMINIOS_INVALIDOS:
        return False, "dominio_desechable_o_typo"
    if not dominio_acepta_correo(dominio):
        return False, "dominio_sin_mx"
    return True, "ok"


def main():
    init_db()
    with conexion() as con:
        leads = [dict(r) for r in con.execute(
            """SELECT id, nombre, email FROM leads
               WHERE email IS NOT NULL AND email != ''
                 AND estado IN ('con_email', 'auditado', 'redactado')"""
        ).fetchall()]

    print(f"Emails a validar: {len(leads)}")
    validos, descartados = 0, 0

    for i, lead in enumerate(leads, 1):
        ok, motivo = validar(lead["email"])
        if ok:
            validos += 1
            print(f"[{i}/{len(leads)}] {lead['nombre'][:38]:38} "
                  f"{lead['email']:42} OK")
        else:
            descartados += 1
            actualizar_lead(lead["id"], email=None, email_fuente=None,
                            estado="sin_email")
            print(f"[{i}/{len(leads)}] {lead['nombre'][:38]:38} "
                  f"{lead['email']:42} DESCARTADO ({motivo})")
        time.sleep(0.05)

    print(f"\nVálidos: {validos} · Descartados: {descartados}")
    print("Resumen:", stats())


if __name__ == "__main__":
    main()
