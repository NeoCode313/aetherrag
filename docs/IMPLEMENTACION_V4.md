# Estabilización v4: acceso, ingesta y proveedores

## Problema y decisiones

El fallo de ingesta provenía de un índice activo `layout-v2` frente a un proceso `layout-v3`. Cambiar una etiqueta no reconstruye los puntos. La recuperación crea una generación separada, reingesta las fuentes, verifica cobertura y cantidad de puntos y realiza un cutover condicionado. Si falta una fuente o falla un trabajo, mantiene la generación anterior. No elimina PDFs ni colecciones antiguas. SQLite recibe una copia del catálogo; PostgreSQL requiere mantener el respaldo operativo de datos y volúmenes.

Acceso e ingesta son operaciones distintas. El cliente limpia los errores anteriores al iniciar sesión. El alta inicial crea un único propietario con Argon2 y contraseña propia; después cierra el registro. La prueba necesita un entorno reproducible por evaluador. Una plataforma SaaS con registro público, correo y permisos por documento está fuera de este alcance.

En desarrollo, el alta directa exige cliente y Host de loopback. Rechaza un Origin diferente y un Host externo para limitar CSRF y DNS rebinding. El token privado queda como mecanismo alternativo del administrador; el usuario de localhost no necesita manipularlo. En producción el propietario se prepara por CLI. No existen contraseñas predeterminadas ni se reutilizan credenciales SSH.

La interfaz identifica producto, huella del código y compatibilidad del índice. Configuración y datos se resuelven desde la raíz, conservando el catálogo legado. Dos catálogos ambiguos producen un error explícito.

## Proveedores y confidencialidad

| Protocolo | Implementación | Configuración |
|---|---|---|
| Chat Completions compatible | OpenAI, Groq, xAI y servidores compatibles | URL base, modelo, clave si corresponde, JSON Schema o JSON básico |
| Ollama | `/api/chat` nativo | URL del servidor y modelo instalado |
| Gemini | `models.generate_content` | Clave y modelo disponible en la cuenta |
| Anthropic Messages | `/v1/messages`, `output_config.format` | Clave y modelo con salida estructurada |
| Extractivo local | Sin API ni interpretación de píxeles | Recorrido reproducible sin una cuenta de pago |

Guardar un perfil no certifica su disponibilidad remota. La prueba explícita utiliza datos sintéticos y valida abstención y contrato sin enviar PDFs. La ingesta funciona independientemente del generador. Los dobles HTTP tampoco certifican una cuenta o modelo remoto concreto.

Credenciales y selección se guardan mediante reemplazo atómico en `DATA_DIR/.private/installation.json`. El catálogo conserva solo una copia descriptiva sin claves. API y worker recargan la configuración privada; reiniciar no cambia el generador elegido ni el perfil de embeddings. Una configuración incompleta mantiene disponibles el login y la reparación; la consulta informa el error y no selecciona silenciosamente otro generador.

Las claves no se guardan en localStorage ni se devuelven en respuestas. Las mutaciones requieren sesión y CSRF. Cambiar de destino elimina la clave anterior. Nuevos destinos HTTPS requieren autorización explícita; se rechazan destinos privados arbitrarios y metadata cloud salvo configuración administrativa expresa. Esto no aísla la red de un propietario malicioso: el propietario administra la instalación.

El archivo privado no está cifrado. La protección en reposo depende del usuario del sistema, permisos y backups privados. No compartir el volumen. El contenedor ejecuta la aplicación sin root; el desarrollo escucha en loopback y el despliegue de evaluación usa HTTPS.

## Criterios verificados por las pruebas

1. Alta única, login, sesión persistente, logout y CSRF.
2. Rechazo del alta desde cliente remoto, Origin externo o Host externo.
3. PDF demo real: parser, tablas y figuras, ingesta, consulta, citas e imágenes.
4. Recuperación de catálogo anterior y fallo conservador ante una fuente ausente.
5. Persistencia de selección y credenciales, actualización entre procesos y estabilidad del perfil de embeddings.
6. Configuración incompleta: la interfaz permite repararla.
7. Adaptadores HTTP y errores de validación sin devolver contenido privado.
8. Navegador contra API real, SQLite y Qdrant embebido, con reinicio y login posterior.
9. PDFs independientes del demo: consultas académicas y de mantenimiento, texto literal, aislamiento por documento y abstención ante conceptos ausentes.
10. El historial de consultas se reinicia al completar una nueva ingesta o eliminar un documento; las respuestas anteriores conservan sus citas. Una pregunta completa no hereda otro tema por contener «su». La abstención permite inspeccionar motivo y fragmentos recuperados sin presentarlos como respuesta validada.
11. CI adicional para PostgreSQL y stack Docker HTTPS → API → outbox → Celery → Qdrant. Consultar cada ejecución antes de afirmar que una revisión pasó.

Las pruebas omiten explícitamente PostgreSQL y Docker si faltan sus servicios. Los contratos Bash se ejecutan en Linux. Un resultado local verde no sustituye la prueba del stack.

## Límites

- No existe un adaptador universal. El modelo debe admitir protocolo, formato y modalidades seleccionados. Bedrock con firma AWS, por ejemplo, requiere otro adaptador.
- La prueba del generador comprueba conexión y contrato, no exactitud semántica ni visión.
- Los embeddings por hashing son aproximados, sin equivalencia con un modelo semántico entrenado.
- No se acredita calidad de todos los proveedores sin recursos, modelos y credenciales reales.
- Las comprobaciones de evidencia no certifican implicación semántica. Se conservan los límites de atribución, unidades y evaluación industrial de la matriz de requisitos.
- La recuperación requiere dejar terminar la operación, conservar respaldos y revisar su estado antes de retirar generaciones anteriores.

## Referencias técnicas

- [Gemini: salidas estructuradas](https://ai.google.dev/gemini-api/docs/structured-output).
- [Anthropic: salidas estructuradas](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
- [Ollama: formato estructurado](https://docs.ollama.com/capabilities/structured-outputs).
- [xAI: JSON Schema](https://docs.x.ai/developers/model-capabilities/text/structured-outputs).

Estas referencias justifican adaptadores separados y validación local completa incluso ante subconjuntos de JSON Schema.
