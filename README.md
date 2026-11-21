# InmoVeritas — revisión inicial de expedientes inmobiliarios (Tiendas 3B)

Sistema multiagente (LangChain + LangGraph, modelo local con Ollama) que recibe documentos mediante un formulario compartible,
los valida, los cifra, los analiza con 10 agentes especializados y presenta al abogado una recomendación inicial
(**Cumple / Requiere revisión / No cumple**) con la evidencia (documento, página y fragmento) de cada hallazgo.
No sustituye la decisión del abogado.

## Puesta en marcha (Windows)

```powershell
cd InmoVeritas
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip uninstall -y onnxruntime
.\.venv\Scripts\python.exe -m pip install --force-reinstall onnxruntime-directml   # OCR por GPU (opcional pero ~5x mas rapido)
.\.venv\Scripts\python.exe scripts\setup_env.py         # genera .env con claves nuevas (no se imprimen)
.\.venv\Scripts\python.exe scripts\create_user.py       # cuenta del abogado + QR TOTP
ollama pull qwen2.5:7b-instruct
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Abra `http://127.0.0.1:8000/panel`, ingrese con contraseña + código TOTP, genere un enlace de recepción y compártalo.
Pruebas: `.\.venv\Scripts\python.exe -m pytest tests -q` (requieren Ollama y el dataset en `../contexto` para las que lo usan).

## Arquitectura

```
Formulario público /r/{token}  ──►  Recepción técnica (sin LLM)
   magic bytes · extensión · tamaño · integridad · ZIP seguro · SHA-256 · cifrado AES-256-GCM
                                      │ evento «expediente recibido» (automático, sin acción del abogado)
                                      ▼
 A1 Coordinador (LangGraph)
   lectura: texto nativo (PyMuPDF) u OCR local (RapidOCR) tras evaluar calidad de imagen
   └► A2 Clasificador por contenido (por documento, en paralelo)
       └► A3 Escrituras ‖ A4 Predial ‖ A5 Alineamiento ‖ A6 Uso de suelo (solo los tipos presentes, en paralelo)
           └► A7 Validador (reglas YAML)  ‖  A8 Comparador entre documentos
               └► A9 Auditor de evidencia (determinista + semántico)
                   └► A10 Evaluador / recomendación ──► A1 consolida ──► panel del abogado /panel
```

| Agente | Archivo | Usa LLM | Qué hace |
|---|---|---|---|
| A1 Coordinador | `agents/coordinador.py` | No (orquesta) | Grafo LangGraph, paralelismo, registro y consolidación |
| A2 Clasificador | `agents/clasificador.py` | Sí | Tipo documental por contenido + confianza |
| A3–A6 Extractores | `agents/extractor_*.py` | Sí | Campos con página y fragmento literal; A6 evalúa compatibilidad tienda/CEDIS |
| A7 Validador | `agents/validador.py` | No (reglas deterministas) | Aplica `config/rules_config.yaml`: faltantes, año predial, vigencias, uso de suelo |
| A8 Comparador | `agents/comparador.py` | Solo en casos ambiguos | Dirección, titular, superficie, clave catastral entre documentos |
| A9 Auditor | `agents/auditor.py` | Solo en valores dudosos | Verifica que cada fragmento exista en el texto original y corrige la página |
| A10 Evaluador | `agents/evaluador.py` | Redacción | Veredicto por criterios del YAML; el LLM solo redacta la explicación |

Cada agente tiene identificador, prompt propio, salida estructurada (Pydantic), reintentos, manejo de errores y registro de ejecución con tiempos
(visible en el detalle del expediente). A7 y A10 (veredicto) son deterministas a propósito: así las reglas no dependen del criterio del modelo.

## Reglas (`config/rules_config.yaml`)
Se pueden reemplazar desde el panel («Reglas»): se valida la estructura, se guarda copia y historial, y cada análisis conserva la versión y huella usadas.
**Lo que NO está definido oficialmente queda vacío** (categorías de uso de suelo, tolerancia de superficie, duración de vigencias) y el sistema lo marca como
pendiente de revisión, no como incumplimiento. Provisional: predial ≥ 2020 (criterio informado por una representante de Tiendas 3B; confirmar con el comité) y los
términos de búsqueda de uso de suelo (no son categorías jurídicas).

## Seguridad: qué sí y qué NO
- Sí: AES-256-GCM por expediente (clave aleatoria, nonce único, AAD por expediente), clave del expediente envuelta con la clave maestra y guardada aparte,
  MFA TOTP (secreto cifrado), contraseñas con bcrypt, bloqueo por intentos, enlaces firmados con vencimiento (7 días), purga con confirmación + MFA y constancia
  sin datos personales, OCR y LLM locales (ningún documento sale del equipo).
- Limitaciones del MVP (no es seguridad de nivel empresarial): la clave maestra está en `.env` en el mismo equipo (no hay KMS/HSM); los bloqueos por intentos y el progreso
  viven en memoria; el formulario público no tiene límite de tasa ni CAPTCHA; no hay HTTPS propio (use un proxy con TLS si se expone en red); el texto extraído se descifra en
  memoria durante el análisis; **la purga no garantiza Zero-Retention** si existen respaldos o copias fuera del control de la aplicación.
- La barra de seguridad solo marca «activo» lo verificado en ese momento (p. ej. cifrado: hay archivos y ninguno en texto plano).

## Rendimiento medido (RTX 5050 8 GB, Ollama qwen2.5:7b, OCR RapidOCR+DirectML)
- Expediente real de 4 documentos (≈6 páginas escaneadas): **≈57–59 s** de extremo a extremo (OCR ≈8 s, clasificación ≈11 s, extracción ≈34 s, resto ≈4 s).
- OCR ≈0.5–1 s por página en GPU (2–13 s en CPU). Escrituras muy largas (>16 páginas) pueden superar los 60 s; se limita a 30 páginas con OCR por documento y el resto queda «no analizado».
- El objetivo de 60 s se cumple en expedientes de tamaño típico, no se garantiza para documentos largos.

## Limitaciones conocidas
- RapidOCR omite espacios entre palabras («DESANJERONIMO»); el modelo lo tolera pero puede afectar fragmentos. El OCR de Windows (`OCR_ENGINE=windows`) separa palabras pero confunde dígitos.
- El Extractor de Alineamiento (A5) recupera menos campos que los demás (p. ej. número oficial) en algunos formatos; conviene revisar su prompt con más muestras.
- Modelo de 7B: puede confundir campos; por eso A9 verifica las citas y los hallazgos graves exigen evidencia verificada para ser «No cumple».
- Integraciones externas (Google Forms, correo, sistemas corporativos) quedaron fuera del MVP; el formulario es nativo.
- No se evaluó formalmente falsos positivos/negativos contra dictámenes de referencia (no estaban disponibles).
