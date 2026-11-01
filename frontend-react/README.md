# React workbench

Cliente React 18 con TypeScript estricto y Vite. El estado de sesión, las consultas y las credenciales se mantienen en memoria; el propietario y la configuración privada siguen perteneciendo al backend.

## Compilación

Con Node 22.12 o posterior:

~~~bash
npm ci --prefix frontend-react --ignore-scripts
npm run build --prefix frontend-react
~~~

El resultado reemplaza únicamente frontend/index.html, frontend/app.js y frontend/styles.css. Swagger, sus recursos locales y las pruebas existentes se conservan. El bundle incluye React y no necesita CDN ni un servidor Node en producción.

La entrada TSX se compila como IIFE y se publica con un HTML externo sin scripts inline. Los nombres de archivo son deterministas; los parámetros de versión se calculan a partir del contenido para que el navegador revalide los bundles tras una actualización. La CI reconstruye los artefactos y comprueba que coincidan con los versionados.

Para desarrollo con recarga, iniciar el backend en loopback y ejecutar npm run dev --prefix frontend-react. Vite mantiene los endpoints API en el mismo origen mediante proxy. La validación de CSP se realiza sobre el build de producción.

## Organización

- components: presentación, modales, mensajes y visor espacial.
- hooks/useWorkbench: coordinación de sesión, consultas, ingesta y selección documental.
- hooks/useActivityPulse: operaciones concurrentes, reloj de 100 ms y estado por latencia.
- hooks/useChatScroll: seguimiento del final y conservación de la lectura manual.
- services/apiClient: transporte, CSRF, errores y cancelación.
- services/sanitizer: Markdown sanitizado y referencias locales.
- types/api: contratos de transporte.

Los metadatos se representan como texto JSX. Solo el cuerpo Markdown utiliza HTML previamente sanitizado. Las citas se crean después de sanitizar; las imágenes solo aceptan rutas de assets/páginas del mismo origen. Una sesión expirada elimina el contenido privado de la interfaz y no reenvía automáticamente la consulta. Un cambio de corpus invalida el contexto aunque el usuario vuelva a la selección anterior antes de terminar una petición.

## Docker y pruebas

Dockerfile compila este cliente en una etapa Node fijada por digest. La imagen final Python contiene el cliente estático y lo sirve mediante FastAPI y Caddy. Docker Compose mantiene API, cliente, Celery, Redis, Qdrant y PostgreSQL bajo el mismo comando; no se necesita Node en el host del evaluador.

~~~bash
docker compose up --build -d
python -B -m pytest -q -p no:cacheprovider
node --test frontend/tests/*.spec.cjs
~~~

Preparar primero las credenciales propias con start-local.ps1 o start-local.sh según el README principal. Las pruebas de navegador requieren Playwright/Chromium. Los servicios PostgreSQL y Docker se verifican en el trabajo separado de CI cuando no están disponibles en el host.

Los indicadores de citas y recuperación describen controles y fuentes; no certifican veracidad semántica. El soporte visual y la capacidad generativa dependen del proveedor configurado.
