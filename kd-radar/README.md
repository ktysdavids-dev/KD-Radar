# KD Radar — Prospección B2B de restaurantes (Valencia + Alicante)

Pipeline completo: **Google Places API → extracción de emails (aviso legal) →
auditoría digital → email personalizado con Claude → envío diario vía n8n**
con cumplimiento LSSI integrado (identificación, baja en un clic, lista de
exclusión permanente).

## 1. Requisitos previos

| Servicio | Qué hacer | Coste orientativo |
|---|---|---|
| Google Cloud | Habilitar **Places API (New)** + crear API Key restringida a esa API | SKU Text Search: cuota gratuita mensual limitada; después ~30-40 USD/1.000 peticiones. Las 54 localidades ≈ 300-350 peticiones. **Verifica precios actuales en tu consola.** |
| Anthropic | API Key en console.anthropic.com | ~1-2 céntimos por email redactado |
| SMTP | Recomendado: subdominio dedicado (p. ej. `mail.ktysdavids.com`) con **SPF + DKIM + DMARC** configurados. Brevo, Resend o Amazon SES | Gratis o céntimos |
| n8n | Tu instancia habitual (self-hosted o cloud) | — |
| Railway | Desplegar `servidor.py` (necesario para que funcionen los enlaces de baja) | Plan actual |

## 2. Instalación

```bash
cd kd-radar
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # rellenar claves
python db.py           # crea kd_radar.db
```

## 3. Ejecución del pipeline (en orden)

```bash
# Prueba primero con UN municipio para validar todo el flujo:
python 1_buscar_negocios.py Gandia

python 2_extraer_emails.py     # aviso legal > contacto > portada
python 3_auditar_digital.py    # detecta pain points reales
python 4_generar_emails.py     # Claude redacta asunto+cuerpo únicos

# Cuando el flujo esté validado, lanza todas las localidades:
python 1_buscar_negocios.py
```

## 4. Servidor (Railway)

```bash
# Local
uvicorn servidor:app --reload --port 8000
# Railway (Start Command)
uvicorn servidor:app --host 0.0.0.0 --port $PORT
```

Actualiza `BASE_URL` en `.env` con el dominio de Railway **antes** de ejecutar
el paso 4 (los enlaces de baja se generan con esa URL).

> Nota Railway: SQLite necesita un **volumen persistente** montado, o migra
> `db.py` a PostgreSQL cuando el volumen de leads lo justifique.

## 5. n8n

1. Importa `n8n_workflow.json`.
2. Sustituye `TU-DOMINIO-RAILWAY` en los dos nodos HTTP.
3. Configura la credencial SMTP.
4. Activa el workflow. Envía L-V a las 9:30, un email cada 1-3 minutos
   (aleatorio), máximo `LOTE_DIARIO` al día.

## 6. Warmup del dominio (crítico para no acabar en spam)

- Semana 1: `LOTE_DIARIO=25` · Semana 2: 40 · Semana 3: 60 · Semana 4: 80-100.
- Nunca desde tu buzón principal: usa subdominio dedicado.
- Vigila rebotes: si superan el 3-4%, para y limpia la lista.

## 7. Marco legal (resumen operativo — no es asesoramiento jurídico)

- **LSSI Art. 21**: el email comercial no solicitado está restringido también
  en B2B. Este sistema **reduce** el riesgo, no lo elimina:
  - Solo buzones **genéricos corporativos** (info@, reservas@) publicados por
    el propio negocio en su web/aviso legal. Nunca emails personales.
  - Identificación completa del remitente y de la empresa en cada email
    (Art. 20-21), pie legal automático.
  - **Baja en un clic** (`/baja/{token}`) con exclusión permanente e
    inmediata; el lote diario filtra contra la tabla `exclusiones`.
  - Un solo email personalizado; sin cadenas agresivas de follow-up
    automatizado.
- **Google Maps/Places ToS**: prohibido scrapear Maps. Aquí se usa la API
  oficial y los emails salen de las webs públicas de los negocios, no de
  Google. Los datos de Places (salvo `place_id`) tienen restricciones de
  caché en los ToS: úsalos como lista de trabajo viva, no como base de datos
  a revender.
- Los leads `sin_web` y `sin_email` son el territorio de **Toni**: llamada
  B2B a número publicado (permitida, manteniendo registro de oposición).

## 8. Métricas

`GET /api/stats` devuelve el embudo completo:
`nuevo → con_email → auditado → redactado → enviado / excluido / sin_web / sin_email`
