# Publicación y actualizaciones privadas

La rama predeterminada de entrega es v4. El repositorio remoto contiene una copia saneada del código. Los secretos, datos y configuración de cada instalación se mantienen fuera de Git.

## Publicar desde la PC de desarrollo

En el entorno original de desarrollo, el propietario dispone de publish-private.ps1 y de submission/repository, un Git independiente. No se publica el historial privado del proyecto original.

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\publish-private.ps1 -Message "fix: describir el cambio realizado"
~~~

El asistente selecciona archivos de una lista permitida, sustituye el README privado por el de entrega, analiza secretos con Gitleaks, ejecuta pruebas locales y crea un commit en la copia de entrega antes de subir v4. Exige que el remoto sea privado y que la copia esté limpia antes de comenzar. No hace force push.

Si falla un control, revisar los cambios que quedaron preparados en submission/repository. No publicar manualmente esa copia sin resolver el control. Los tests locales omiten PostgreSQL/stack cuando no están configurados; revisar también la CI de GitHub.

Los scripts privados de exportación/publicación no se incluyen en el código que reciben los evaluadores. Un desarrollador que trabaje directamente en un clon limpio puede usar el flujo Git habitual, conservando las mismas revisiones de secretos y pruebas.

## Acceso de lectura desde el servidor

Crear una clave exclusiva del repositorio dentro del servidor. La clave privada permanece allí:

~~~bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/aetherrag-github -C aetherrag-deploy
cat ~/.ssh/aetherrag-github.pub
~~~

Si el archivo ya existe, conservarlo o elegir otro nombre; no sobrescribir una clave utilizada. Elegir una contraseña de clave si se va a desbloquear manualmente con ssh-agent.

Añadir únicamente el contenido del archivo .pub en GitHub → repositorio → Settings → Deploy keys. Mantener desmarcada la autorización de escritura. No usar la clave de acceso al servidor ni copiar su clave privada a GitHub.

Verificar la identidad SSH de GitHub con sus [huellas oficiales](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) al conectar por primera vez. No desactivar StrictHostKeyChecking.

El repositorio privado creado es NeoCode313/aetherrag. Si la nueva clave tiene passphrase, desbloquearla en esa sesión del servidor:

~~~bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/aetherrag-github
~~~

Clonar y fijar la identidad SSH exclusiva del repositorio:

~~~bash
git -c core.sshCommand="ssh -i $HOME/.ssh/aetherrag-github -o IdentitiesOnly=yes" \
  clone git@github.com:NeoCode313/aetherrag.git ~/apps/aetherrag-source

git -C ~/apps/aetherrag-source config core.sshCommand \
  "ssh -i $HOME/.ssh/aetherrag-github -o IdentitiesOnly=yes"
~~~

GitHub documenta que una [deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys) se asocia a un repositorio. Su acceso debe limitarse a lectura.

## Actualizar la instalación existente

Conservar fuera del clon la carpeta de configuración de la instalación que ya funciona: debe contener su .env y su compose.server-test.yml. No copiar un .env del autor o del evaluador.

Antes de desplegar, comprobar la CI de la revisión deseada en GitHub. El actualizador no consulta automáticamente el resultado de la CI.

Para la instalación existente de v3:

~~~bash
cd ~/apps/aetherrag-source
bash ops/update-server.sh ~/apps/aetherrag-v3-f305946 aetherrag-v3-test origin/v4
~~~

Para fijar una revisión aprobada, sustituir origin/v4 por su SHA. El script:

1. Requiere un clon limpio y una instalación existente; bloquea actualizaciones concurrentes.
2. Descarga main y comprueba que la revisión pertenece a esa rama.
3. Detiene el proceso si cambian los archivos de esquema/migraciones: eso requiere un plan de migración y respaldo separado.
4. Construye una imagen etiquetada por commit.
5. Mantiene el nombre del proyecto Docker y los volúmenes existentes.
6. Recrea únicamente API, worker y proxy, conserva los límites del overlay privado y espera su disponibilidad.
7. Comprueba que /docs entregue los recursos locales corregidos.
8. Ante error, intenta recuperar la imagen y revisión anteriores. Informa si hay que revisar la recuperación; no promete una reversión de datos.

La instalación inicial de este flujo presupone el esquema v3 que ya tiene el servidor. No está diseñada para migrar automáticamente instalaciones antiguas ni hacer cambios destructivos de datos.

La carpeta privada incorpora .deploy-state para bloqueo, overlay de imagen e historial. No se publica. Las otras aplicaciones Docker del servidor quedan fuera del proyecto seleccionado.

Durante la recreación habrá una interrupción breve. Para minimizar el tiempo, primero se construye la imagen y después se recrean los contenedores.

## Comprobaciones después de actualizar

Abrir /docs desde el mismo origen que la aplicación. En la instalación con túnel privado usar el puerto local de ese túnel; el script no abre puertos públicos.

Una respuesta HTML que todavía incluya cdn.jsdelivr.net corresponde al Swagger antiguo. La versión corregida incluye /static/api-docs-init.js. Recargar el navegador no sustituye reiniciar el backend o reconstruir un contenedor antiguo.

Probar login, una consulta, su cita y la figura/página asociada. Para las pruebas completas del stack, usar la contraseña propia de la aplicación sin guardarla en Git.

No se configura una conexión entrante desde GitHub Actions al servidor. La publicación y la actualización se mantienen como dos pasos explícitos, con las claves del servidor protegidas.
