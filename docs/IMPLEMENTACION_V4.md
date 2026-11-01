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

## Corrección de consultas v4.1

Las dos regresiones del manual demo recuperaban la información necesaria, pero la descartaban antes de mostrarla. El proveedor podía declarar un modo extractivo para una respuesta generativa o mencionar un alias visual no enviado. Ahora el adaptador fija el modo, solicita únicamente afirmaciones, referencias y aspectos faltantes, y el servidor calcula los campos derivados. El esquema limita los alias a la evidencia disponible y a las imágenes efectivamente adjuntas.

Cada afirmación se verifica de forma independiente. Una referencia visual inválida no elimina hechos textuales sustentados. La comprobación conserva las diferencias entre cita literal, descripción extraída y observación visual real. Las citas aceptan normalización determinista de espacios y ligaduras PDF, conservando el tramo original. Un extracto inequívoco de una celda hereda exclusivamente su propia fila; los identificadores de figura se comprueban contra el encabezado de la misma fuente. Una fuente en prosa que sustenta por sí sola la afirmación no deja de ser válida al añadir una referencia de tabla.

Las peticiones explícitas de figura, tabla o página incorporan contexto de layout bajo los mismos filtros de documento y publicación que la búsqueda híbrida. Se conservan las partes de la tabla pedida. Existe una regeneración acotada cuando ninguna afirmación supera los controles; no se relajan los controles para aceptar la segunda salida.

La selección de PDFs evita mezclar manuales y reinicia únicamente el contexto de nuevas consultas. Los resultados históricos conservan sus referencias. El manual demo se cachea con una huella de su plantilla para no reutilizar indefinidamente un PDF generado con una versión anterior.

Se incorpora recuperación semántica entrenada con multilingual-e5-small, inferencia ONNX en CPU y prefijos query/passage. Los pesos y el tokenizer se fijan por revisión y SHA-256. La división utiliza el tokenizer real, con 480 tokens por fragmento para reservar contexto. La reconstrucción de índices existentes conserva los PDFs y crea una generación nueva antes del cutover. La selección de embeddings no sustituye al generador elegido.

Los errores 429 conservan un Retry-After numérico y lo respetan dentro de un presupuesto acumulado de espera de 30 segundos. No se reintenta una espera de cuota diaria. Un límite de cuenta sigue siendo un error de proveedor, no una falsa afirmación de falta de evidencia. La salida se limita a 1600 tokens para evitar reservar el contexto completo. Se retira de la interfaz el preset Groq que devolvió 404; el modelo sigue siendo editable.

### Evidencia de esta corrección

- Se ejecutaron las dos preguntas originales de las capturas contra Groq con qwen/qwen3.8-27b, enviando únicamente el manual sintético generado por el proyecto. Ambas devolvieron respuestas completas, citas y figura relacionada; no se atribuyó interpretación de píxeles al modelo de texto.
- Figura 1 y Tabla 1: seis componentes, potencia nominal 145 MW y salto nominal 182 m.
- E-409 y Figura 3: presión inferior a 115 bar, componentes del circuito, verificación de precarga de N2 de 140 bar y fugas en V-12B.
- Los contratos comprueban referencias visuales inválidas, paráfrasis, normalización de citas, mezcla de filas, magnitudes y cuotas.
- Las pruebas con Qdrant comprueban figuras y partes de tablas, excluyendo documentos ajenos y ejecuciones no publicadas.
- Las pruebas optativas con el modelo E5 real comprueban paráfrasis sin coincidencia de identificadores, norma L2 y migración con persistencia tras reinicio.
- Las pruebas de navegador incluyen alta, login posterior al reinicio, demo, consulta con citas, prueba de proveedor y Swagger bajo CSP.

No se enviaron PDFs privados a proveedores externos para estas comprobaciones. La prueba real de dos preguntas no certifica todos los modelos ni todos los manuales. Los contratos HTTP de otros adaptadores tampoco prueban sus cuentas externas. La interpretación visual requiere un modelo compatible y debe evaluarse con preguntas que dependan de los píxeles.
