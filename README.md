# AetherRAG — entrega técnica reproducible

Aplicación para PDFs técnicos con ingesta asíncrona, recuperación híbrida, citas y asociación espacial de figuras. El cliente React 18 + TypeScript se compila con Vite y se sirve desde FastAPI. Esta entrega se ejecuta en el equipo del evaluador: no requiere acceso a servidores del autor ni credenciales compartidas.

## Requisitos

- Docker Engine con Compose o Docker Desktop con contenedores Linux.
- Compose 2.20 o posterior y acceso a Internet para descargar imágenes y dependencias.
- Windows necesita WSL 2/virtualización funcionando; también puede usarse Linux nativo.
- Como punto de partida, disponer de 4 GB de RAM para Docker y espacio para las imágenes. El consumo depende de los PDFs.
- El stack se probó en Linux x86_64. No se afirma una prueba directa en todas las combinaciones de Windows/macOS/ARM.

Python del host y API keys son opcionales para el arranque con los scripts siguientes.

## Iniciar

Windows, desde PowerShell en la carpeta del proyecto:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-local.ps1
~~~

ExecutionPolicy Bypass se aplica solo a ese proceso para ejecutar el script revisable de la entrega; no cambia la política persistente de Windows.

Linux/macOS:

~~~bash
bash start-local.sh
~~~

El script construye la imagen, solicita una contraseña propia del evaluador, crea .env con secretos aleatorios y arranca Compose. Un .env existente se conserva. Si algún paso falla, detiene la secuencia.

La documentación interactiva está en https://localhost:8443/docs y usa recursos locales, sin CDN.

Abrir https://localhost:8443 e iniciar sesión con admin y la contraseña elegida. Subir un PDF propio o usar el botón de manual de ejemplo para recorrer el flujo; el ejemplo es sintético. El certificado usa una CA local generada por esa instalación. Para HTTPS sin aviso, configurar su confianza siguiendo la documentación oficial de Caddy: https://caddyserver.com/docs/running#local-https-with-docker . No se incluyen certificados ni claves del autor.

Si el puerto está ocupado, agregar APP_HTTPS_PORT=9443 a .env y volver a iniciar; usar https://localhost:9443. Solo se publica la interfaz en loopback, no las bases de datos.

## Prueba del stack

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-local.ps1 -Action test
~~~

~~~bash
bash start-local.sh test
~~~

Solicita la contraseña local y ejecuta contratos de catálogo y un recorrido HTTPS real: login, CSRF, upload, worker Celery, consulta, citas y acceso a imágenes/páginas. Se verificó una ejecución de este subconjunto con 52 pruebas correctas. Esa evidencia no es una certificación de exactitud semántica.

Para detener sin eliminar datos, usar -Action stop en Windows o bash start-local.sh stop en Linux.

Para ejecutar toda la suite en el host, instalar requirements.lock con --require-hashes y ejecutar python -B -m pytest backend/tests. Los contratos PostgreSQL y del stack requieren configurar sus entornos de prueba. Las pruebas de navegador requieren Node, Playwright y Chromium.

## Arquitectura y decisiones

~~~mermaid
flowchart LR
  UI[React 18: chat y visor] --> TLS[Caddy HTTPS]
  TLS --> API[FastAPI]
  API --> SQL[(PostgreSQL: jobs y outbox)]
  API --> Query[Recuperación híbrida y evidencia]
  Dispatcher[Dispatcher] --> Redis[(Redis)]
  Redis --> Worker[Celery]
  Worker --> PDF[PyMuPDF y chunking de layout]
  PDF --> Qdrant[(Qdrant)]
  Worker --> SQL
  Query --> Qdrant
  Query --> LLM[Proveedor intercambiable]
~~~

- Dominio, aplicación, infraestructura y transporte separados mediante puertos.
- PostgreSQL decide publicación; outbox, leases y fencing coordinan trabajos repetidos y recuperación.
- Cada intento escribe assets y puntos aislados; un fallo conserva la publicación anterior.
- Chunking por secciones, párrafos y tablas, con presupuesto completo y correlación de figuras.
- Recuperación semántica con multilingual-e5-small y léxica TF con hashing, fusionadas por RRF. No se afirma BM25 convencional.
- Respuestas con referencias explícitas, validación estructural y comprobaciones numéricas limitadas.
- Sesión del operador, Argon2, CSRF, DOMPurify y autorización de assets.

## Cliente React y compilación reproducible

El código fuente está en frontend-react/. Dockerfile usa una etapa Node con digest fijo y npm ci para compilar TypeScript y Vite. La imagen final sirve los archivos estáticos del cliente y la API desde el mismo origen; Node no se ejecuta en producción. Un solo docker compose up --build -d arranca API, cliente y servicios auxiliares después de preparar el .env propio de la instalación.

Los artefactos en frontend/ también se versionan para ejecutar el backend nativo. La CI los reconstruye y verifica que coincidan con el código fuente. Las rutas deterministas incorporan una huella de contenido para evitar bundles antiguos en la caché del navegador. Para editar la UI con Node 22.12 o posterior: npm ci --prefix frontend-react y npm run build --prefix frontend-react. Las instrucciones completas están en frontend-react/README.md.

La migración React está aislada en v4-react; la rama v4 conserva la implementación anterior. Los tests originales de navegador se mantienen y se añaden contratos de concurrencia, caducidad de sesión, credenciales y desplazamiento.

## Modelos y secretos

La instalación nueva utiliza embeddings semánticos multilingües E5 ejecutados localmente en CPU. La imagen Docker descarga una revisión fija del modelo y su tokenizer (aproximadamente 135 MB) y verifica sus SHA-256 durante la construcción. No descarga un LLM generativo ni envía documentos al servicio de distribución de pesos. El generador predeterminado sin API devuelve extractos literales y no interpreta píxeles. Los adaptadores Gemini GenerateContent, Anthropic Messages, Ollama nativo y Chat Completions compatible (OpenAI, Groq, xAI, LM Studio y vLLM) permiten usar las credenciales y modelos del evaluador.

Para explicaciones redactadas, configura un generador local o externo. Un modelo de texto puede responder desde párrafos, tablas, leyendas y etiquetas, y mostrar la figura asociada. Solo un modelo con visión habilitada recibe imágenes y permite interpretar rasgos que únicamente existen en los píxeles. La interfaz distingue figuras relacionadas de imágenes efectivamente enviadas.

En una instalación anterior con embeddings por hashing, usa «Activar búsqueda semántica». La reconstrucción mantiene fuentes e índice anterior, verifica los documentos y activa una generación separada; no cambia el generador ni sus credenciales. No cierres la aplicación mientras termina. Si falta alguna fuente, conserva el índice anterior y comunica el fallo. Para desarrollo nativo, ejecuta primero python backend/scripts/download_semantic_model.py o usa start-dev.ps1.

Marca en el corpus los PDFs que quieras consultar. Sin selección se consulta el corpus completo. Una ingesta completada selecciona el documento recién cargado y reinicia el contexto de preguntas.

El propietario configura proveedor, modelo, endpoint y clave desde Generador de respuestas. Las claves se limpian del formulario y se guardan en el volumen privado; no se devuelven por la API ni se incluyen en Git. Probar el proveedor guardado comprueba conexión y contrato con datos sintéticos, sin enviar PDFs. Puede consumir cuota de la cuenta elegida.

Usa la URL base hasta /v1 para APIs compatibles; para Ollama nativo usa la URL del servidor sin /api/chat. Desde Docker, localhost señala el contenedor: usa host.docker.internal para un modelo en el PC. En Linux Compose incorpora host-gateway. El runtime local debe escuchar en una interfaz accesible desde Docker, con su firewall limitado al acceso necesario.

Los destinos HTTPS personalizados requieren autorización explícita. Cambiar el endpoint elimina la clave asociada al anterior. JSON básico permite modelos sin JSON Schema, manteniendo validación estricta. Habilita imágenes solo si el modelo las admite. No se sustituye silenciosamente el generador por el modo local cuando falla una API.

No se proporciona una contraseña de aplicación predeterminada. PostgreSQL, Redis, Qdrant y sesiones reciben secretos distintos, nuevos en cada instalación. Las contraseñas del sistema operativo/SSH no se solicitan ni se utilizan por la aplicación.

Para cambiar la contraseña de aplicación sin Python en el host:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-local.ps1 -Action password
~~~

~~~bash
bash start-local.sh password
~~~

Solicita una contraseña nueva y recrea API/worker. También cambia la clave de firma de sesiones e invalida las sesiones actuales, sin rotar las credenciales de infraestructura. Elegir una clave diferente de SSH y de cualquier otro servicio: la aplicación no conoce esas claves y no puede compararlas.

La CA local es una autoridad de confianza para el usuario del sistema, no solo para esta aplicación. Su clave privada debe permanecer en el volumen local de Caddy. No importar certificados recibidos del autor ni compartir ese volumen; para la evaluación también puede aceptarse la excepción de localhost en un perfil de navegador dedicado, según la política del equipo.

No subir .env, datos, volúmenes, certificados, logs o credenciales al repositorio. .env.example contiene valores de referencia sin secretos reales. Los tokens que aparecen en fixtures de prueba son datos sintéticos de test, no credenciales de servicios.

## Estado y límites conocidos

- Los controles deterministas no garantizan implicación semántica: permanecen casos de unidades no reconocidas y atribución entre entidades que pueden pasar la validación.
- Los índices antiguos requieren reconstrucción y cutover verificado. La recuperación conserva fuentes y colecciones anteriores; una fuente ausente impide activar un corpus incompleto.
- La evaluación sintética comprueba términos y ubicaciones, no corrección completa de parámetros ni visión real.
- No se acredita carga con 100 manuales industriales complejos, ni un SLA de modelos remotos.
- La aplicación tiene un operador administrador; no es una plataforma multiusuario con permisos por documento.

Los datasets de regresión están en backend/evaluation. Los informes privados de desarrollo y la configuración de infraestructura del autor no forman parte de esta entrega.

## Uso y licencia

La entrega se prepara para evaluación privada. No se ha seleccionado una licencia de distribución pública para el código propio. Las licencias de las bibliotecas vendorizadas se conservan en frontend/vendor.

## Actualizaciones desde GitHub

El flujo de publicación y actualización del servidor está en docs/DEPLOYMENT.md. Los secretos y volúmenes no forman parte del repositorio. La guía de cumplimiento está en docs/REQUISITOS_CUMPLIMIENTO.md.

## Desarrollo nativo y alta inicial

Docker se evalúa con los comandos anteriores. El modo nativo es una instalación independiente:

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
Copy-Item .env.example .env
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-dev.ps1
~~~

No sobrescribas un .env existente. Abre http://localhost:8000/ y elige usuario y contraseña de al menos 15 caracteres. No hay contraseña compartida ni código que debas inventar. El alta directa exige cliente y Host de loopback, rechaza un Origin diferente y queda cerrada tras el primer propietario. En producción el propietario se crea por setup_env.py antes de arrancar.

Cada instalación mantiene identidad, firma y cookie propias. Los datos se resuelven desde la raíz del proyecto; se conserva el catálogo nativo legado. Si hay dos catálogos, configura DATA_DIR absoluto para evitar una selección silenciosa.

Recuperación de contraseña nativa desde la cuenta del sistema que administra sus datos:

~~~powershell
.\.venv\Scripts\python.exe -B backend/scripts/local_setup.py --reset-password
~~~

El cambio invalida sesiones anteriores. Docker utiliza start-local.ps1 -Action password o bash start-local.sh password.

La interfaz y /api/v1/system/version muestran versión y huella del código. Si el índice pertenece a otra versión, inicia sesión y pulsa Recuperar índice anterior antes de subir PDFs. La identidad del pipeline sigue siendo layout-v3; la versión del producto es 4.0.0.

Decisiones y límites: [implementación v4](docs/IMPLEMENTACION_V4.md).
