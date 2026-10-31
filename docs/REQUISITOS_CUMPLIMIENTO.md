# Cumplimiento de la prueba técnica

Revisión: 31 de octubre de 2026. Esta matriz desglosa los requisitos en 22 comprobaciones: expande las tres obligaciones del README, pero conserva juntos los subpuntos de cada requisito funcional. Es una clasificación operativa propia, no una rúbrica oficial.

Cumple significa que existe implementación y evidencia pertinente; no certifica calidad universal ni producción. Parcial significa que existe una solución pero falta evidencia importante o permanece un defecto material.

| # | Requisito del documento | Estado | Evidencia y límite relevante |
|---|---|---|---|
| 1 | Separar transporte, casos de uso e integraciones | Cumple | dominio, aplicación, infraestructura y presentación separados |
| 2 | Inyección de dependencias e intercambio LLM/vector store | Cumple | puertos y composition root; adaptadores y pruebas con mocks |
| 3 | Variables de entorno y ambientes | Cumple | Settings, .env.example, bootstrap y secretos de instalación fuera de Git |
| 4 | Ingesta en segundo plano | Cumple | Celery/Redis, outbox, leases y recuperación; Background Tasks para desarrollo |
| 5 | job_id y endpoint de progreso/estados | Cumple | subida y seguimiento; pruebas del ciclo de ingesta |
| 6 | Extraer texto, tablas e imágenes de PDFs técnicos complejos | Parcial | PyMuPDF, OCR opcional y fixtures reales construidos; falta validación amplia con manuales complejos independientes |
| 7 | Guardar coordenadas y correlacionar contexto | Cumple | bounding boxes, transformaciones geométricas y spatial linker probados |
| 8 | Indexación semántica/layout y almacenamiento seguro en BD vectorial | Cumple | secciones, párrafos, filas y presupuesto; Qdrant con API key, sin puerto público y con filtrado de documentos/generación |
| 9 | Recuperación híbrida semántica/palabras e imágenes asociadas | Parcial | fusión densa/léxica y asociación presentes; modo local usa hashing aproximado, sin acreditar calidad semántica con corpus independiente |
| 10 | Generación defensiva y admitir información insuficiente | Parcial | prompt, contrato, citas y abstención implementados; siguen fallos de unidades/entidades descritos en el plan de fidelidad |
| 11 | Unit tests con mocks de LLM y vector store | Cumple | pruebas aisladas y bloqueo de HTTP externo accidental |
| 12 | Retries/backoff o circuit breaker | Cumple | errores transitorios y circuit breaker probados; errores permanentes no se disfrazan como modo local |
| 13 | Logging y trazabilidad crítica | Cumple | request ID y logs estructurados; no equivale a una plataforma completa de observabilidad |
| 14 | Chat, historial y Markdown correcto | Cumple | cliente web y pruebas de navegador, sanitización y procedencia histórica |
| 15 | Mostrar la imagen pertinente cuando la respuesta depende de ella | Parcial | correlación espacial, figuras y páginas presentes; falta acreditar interpretación visual real independiente y exactitud de la respuesta visual |
| 16 | Fuente y página explícitas | Cumple | citas y visor enlazados con autorización de assets |
| 17 | GitHub/GitLab, commits semánticos e historial lógico | Cumple | código publicado en NeoCode313/aetherrag, privado; historial saneado y commits reales de las mejoras |
| 18 | Compose funcional con backend, frontend y auxiliares en un comando | Cumple | GitHub Actions construyó esta revisión y ejecutó el stack real: 52 pruebas correctas con PostgreSQL, Redis, Qdrant, Celery, API/frontend y Caddy |
| 19 | README con diagrama de arquitectura | Cumple | diagrama incluido |
| 20 | README con decisiones técnicas | Cumple | estrategias y límites explicados |
| 21 | README con inicialización y pruebas | Cumple | scripts de arranque y comandos de pruebas documentados |
| 22 | Video o despliegue en nube demostrando un documento técnico | Pendiente | el servidor privado no constituye por sí solo una demostración accesible al evaluador; falta entregar video o acceso de demostración adecuado |

## Resultado al preparar la publicación

17 cumplen, 4 son parciales y 1 está pendiente. Esto no es una puntuación de 15/22: los requisitos no tienen el mismo peso y la fidelidad/visualización son centrales en el reto.

La publicación privada del punto 17 quedó confirmada; no se transfirió el historial privado. El punto 18 se cerró con la construcción y prueba real de GitHub Actions; no solo con validación estática.

## API docs: fallo adicional, no requisito literal independiente

La prueba exige API REST y documentación para inicializar/probar, pero no pide Swagger/OpenAPI de manera explícita. Aun así, dejar un enlace funcional que abre una página en blanco es un defecto real de producto.

La revisión local y el servidor seguían entregando Swagger antiguo: ambos HTML incluían CDN mientras Content-Security-Policy permitía solo scripts propios. La corrección incorpora Swagger local y un inicializador externo. Hasta reiniciar el backend local o reconstruir la imagen del servidor, editar archivos no cambia las rutas ya cargadas en memoria.

La comprobación de despliegue debe confirmar que /docs incluye /static/api-docs-init.js, que sus assets devuelven 200 y que Swagger se representa en el navegador con la política de seguridad activa.

## Qué falta para aspirar a 9/10

- Corregir y medir fidelidad técnica; una cita auténtica no demuestra una afirmación correcta.
- Evaluar recuperación y respuestas sobre familias documentales independientes con embeddings adecuados.
- Probar un modelo visual real y preguntas cuya solución dependa de los píxeles, con anotaciones independientes.
- Actualizar la instalación privada desde Git; la construcción y prueba real del stack de esta revisión ya pasaron en GitHub Actions.
- Entregar una demostración accesible sin revelar infraestructura o secretos.
- Reparar el flujo administrativo de mantenimiento/reindexado ya identificado.
- No presentar la carga sintética de 100 documentos de una página como prueba de 100 manuales industriales complejos.

Las métricas de proximidad, términos, tokens o referencias válidas no son una probabilidad de veracidad. No asignaría 9/10 con la evidencia actual.

La API docs local se verificó después de reiniciar su proceso conservando la carpeta de trabajo: Swagger renderiza 15 operaciones sin errores de navegador. La instalación privada también se actualizó mediante el nuevo flujo y /docs se comprobó a través del túnel: HTTP 200, 15 operaciones renderizadas y cero errores de navegador. La primera CI detectó una prueba de despliegue con escapes incorrectos en su mock de logging; se corrigió y la nueva ejecución completó ambos trabajos correctamente.

## Evidencia remota confirmada

[GitHub Actions, ejecución 41247639029](https://github.com/NeoCode313/aetherrag/actions/runs/41247639029), revisión bfe0fed:

- Contratos y suite: 206 casos, 205 correctos, 1 omitido, cero errores/fallos. Incluye PostgreSQL y las tres pruebas Linux del actualizador.
- Stack Docker real: 52 pruebas correctas en 7.32 segundos.
- Ambos trabajos finalizaron con éxito. Los subconjuntos se solapan; no se suman como 257 pruebas únicas.
- La CI también ejecutó la evaluación sintética y el smoke de carga. Esas ejecuciones no convierten los casos sintéticos en evidencia independiente ni validan visión real.
- La CI acredita el código de esa revisión. Después, el propietario ejecutó el actualizador en su servidor y confirmó el resultado; se verificó por el túnel que Swagger funciona. La actualización permanece explícita, no se instaló un despliegue automático por cada push.

La actualización posterior de esta matriz y de la guía de acceso SSH es exclusivamente documental.

La clave pública del servidor se registró en GitHub exclusivamente con permiso de lectura; la clave privada y el .env no se transfirieron al repositorio.
