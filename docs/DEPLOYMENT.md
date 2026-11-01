# Despliegue, actualizaciones y acceso al repositorio

## Instalación independiente

La ruta principal para el evaluador está en [README.md](../README.md): clonar el repositorio autorizado y ejecutar `start-local.ps1` o `start-local.sh`. Cada instalación genera sus secretos y volúmenes. No requiere conectarse a infraestructura del autor.

Los datos, `.env`, CA/certificados, claves de proveedores y backups deben permanecer fuera del historial Git. El archivo `compose.performance.yml` es un perfil portable de recursos; no contiene credenciales ni direcciones de un servidor concreto.

## Publicar cambios de código

En un clon de trabajo con permisos de escritura:

```bash
git status --short
git diff --check
git add <archivos-revisados>
git commit -m "fix: describir el comportamiento corregido"
git push origin <rama-de-trabajo>
```

No añadir `.env`, volúmenes, directorios de datos o logs. Antes de una actualización, comprobar la CI del commit específico y analizar secretos tanto en archivos como en historial. Un análisis de secretos no sustituye revisar datos privados, rutas de infraestructura y documentación.

La rama de entrega es `v4-react`. Las revisiones anteriores permanecen identificables mediante commits; el nombre de rama por sí solo no acredita qué imagen está ejecutándose. El endpoint `/api/v1/system/version` muestra una huella del código y el historial privado de despliegue registra el commit/imagen.

## Lectura de GitHub desde un servidor

Si se necesita actualizar un despliegue desde GitHub, utilizar credenciales propias de ese servidor. Una deploy key de solo lectura puede asociarse únicamente a este repositorio. Nunca trasladar al servidor una clave privada de otra máquina ni incrustar tokens en la URL del remoto.

Ejemplo, ejecutado en el servidor administrador:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/aetherrag-github -C aetherrag-deploy
cat ~/.ssh/aetherrag-github.pub
```

Conservar cualquier clave existente; elegir otro nombre si hace falta. Añadir solo el contenido público en GitHub, ajustes del repositorio, **Deploy keys**, sin permiso de escritura. Verificar la identidad del host usando las [huellas oficiales de GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).

Si la clave tiene passphrase, desbloquearla en la sesión del operador:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/aetherrag-github
```

La passphrase y la clave privada no se documentan ni se comparten. Para información sobre su alcance, consultar [deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys).

## Actualizador de una instalación existente

`ops/update-server.sh` está diseñado para una instalación existente, con proyecto Compose conocido y esquema compatible. No es un asistente de primera instalación ni una migración automática de cualquier versión.

Separa:

- **Clon de código:** contiene Git, Dockerfile, Compose y aplicación.
- **Configuración privada:** directorio fuera del clon con `.env`, un overlay `compose.server-test.yml` y `.deploy-state/`.
- **Volúmenes:** PostgreSQL, Redis, Qdrant, PDFs/estado y Caddy de ese proyecto.

Los siguientes nombres son ejemplos genéricos. Selecciona las rutas y el nombre del proyecto de tu instalación existente:

```bash
APP_SOURCE="$HOME/apps/aetherrag-source"
APP_CONFIG="$HOME/apps/aetherrag-runtime"
APP_PROJECT="aetherrag-evaluation"
cd "$APP_SOURCE"
git fetch origin v4-react
git status --short
```

Antes de activar:

1. Identificar el commit deseado y confirmar sus verificaciones de CI.
2. Esperar a que terminen las ingestas o ejecutar mantenimiento controlado.
3. Crear y comprobar backups coordinados de PostgreSQL, PDFs/estado privado y snapshots de Qdrant. Conservar también configuración, imagen y commit anteriores.
4. Revisar cambios de esquema, perfiles de embeddings y requisitos de memoria. Un cambio de generador no es una migración de embeddings.
5. Verificar que `.env` y el overlay privado pertenecen a esa instalación y que el clon está limpio.

Para cargar por primera vez una versión de la herramienta con soporte para esta rama:

```bash
git switch --detach origin/v4-react
bash ops/update-server.sh "$APP_CONFIG" "$APP_PROJECT" origin/v4-react
```

Ese primer `switch` cambia la referencia de código antes de que la herramienta capture el commit anterior; registra el commit real anterior por separado para una eventual recuperación. En las actualizaciones siguientes basta invocar el actualizador desde el checkout existente.

### Comportamiento del actualizador

- Serializa operaciones mediante un lock privado.
- Descarga la rama seleccionada y comprueba la pertenencia de la revisión.
- Detiene la actualización automática si cambian los archivos de esquema/migraciones.
- Construye una imagen etiquetada por commit antes de sustituir contenedores.
- Conserva proyecto/volúmenes y aplica el overlay privado de recursos.
- Recrea API, worker y proxy, espera su salud y comprueba los assets locales de `/docs`.
- Registra timestamp, commit e imagen en `.deploy-state/history`.
- Ante error, intenta recuperar la imagen/configuración anterior. La recuperación de contenedores no es una reversión de datos.

El script no comprueba por sí solo el resultado de GitHub Actions ni realiza todos los backups anteriores. Tampoco abre puertos públicos, instala webhooks o configura despliegue automático por cada push. Cambios de esquema o de índice necesitan su propio plan de migración y reversión.

## Perfil de recursos

El perfil [compose.performance.yml](../compose.performance.yml) permite probar recursos sin introducir datos privados en Git. Para una instalación local de evaluación, utiliza el comando del README con el mismo proyecto Compose.

En una instalación existente cuyo actualizador ya aplica `compose.server-test.yml`, revisar e incorporar únicamente los límites, comando del worker y variables de rendimiento necesarios al overlay privado. Mantener sus credenciales, rutas de volúmenes y restricciones de red. La RAM más swap debe ser compatible con el límite de RAM; no incrementar solo uno de esos campos a ciegas.

No se debe aumentar el número de procesos de Celery sin presupuestar la duplicación de modelos. El perfil comienza con un worker, varios hilos internos de inferencia y cuotas explícitas. Medir antes/después con [el procedimiento de rendimiento](PERFORMANCE.md).

## Comprobaciones posteriores

- `/api/v1/system/health`: catálogo y vector store disponibles; revisar también `index_compatible`.
- `/api/v1/system/version`: huella y pipeline de la imagen esperada.
- `/`: cliente React y sus assets con huellas de contenido.
- `/docs`: Swagger y sus recursos locales; comprobarlo en el navegador.
- Sesión/CSRF, ingesta de un PDF sintético por el worker y consulta con citas/figuras/páginas.
- Persistencia de selección del proveedor y del perfil del índice tras recrear contenedores.
- PDFs originales, volúmenes y servicios ajenos conservados; ausencia de reinicios/OOM inesperados.

En instalaciones con un túnel privado, la URL de localhost depende de que ese túnel esté activo. No se debe publicar una dirección privada del administrador como URL de evaluación. Para el entregable de demostración, usar un video con datos sintéticos o un despliegue destinado expresamente a los evaluadores.

## Compartir la entrega

Mantener el repositorio privado e invitar cuentas desde **Settings > Collaborators** de ese repositorio. La invitación debe referirse solo al proyecto. No entregar el `.env` del autor, credenciales SSH, certificados, datos o volúmenes. En una cuenta personal, GitHub otorga lectura y escritura a colaboradores de repositorios privados; para un rol exclusivamente **Read** se utiliza un repositorio de organización. Revisar el rol antes de enviar la invitación.

Los evaluadores crean su administrador y proveedor en su instalación. El acceso al repositorio no implica acceso a la aplicación ya desplegada del administrador ni a otros repositorios.
