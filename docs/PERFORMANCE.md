# Evaluación de rendimiento de la ingesta

El objetivo es reducir el tiempo hasta la publicación de un PDF conservando texto, tablas, números, asociaciones, geometría y crops. Se separa la mejora de recursos de los cambios del parser. Este documento no define un SLA.

## Qué se mide

- Preparación del modelo, separada del tiempo de procesamiento.
- Extracción del PDF, captions, chunking, linking, embeddings densos y codificación dispersa.
- En el parser actualizado: texto, OCR, tablas, figuras y previsualizaciones completas.
- Cantidad de páginas, chunks, vectores y figuras; pico de memoria y contadores cgroup cuando están disponibles.
- SHA-256 de la fuente y huella de integridad que incluye texto literal, geometría, asociaciones y los píxeles de las figuras. Se excluyen los archivos de preview diferidos, que se validan por separado frente al render eager.

El benchmark usa el mismo PDF de 24 páginas, generado repitiendo ocho veces el manual sintético de tres páginas del proyecto. Sirve para una comparación controlada; no representa la diversidad de un corpus industrial. Reutilizar el directorio conserva exactamente los mismos bytes fuente.

## Línea base

Mediciones de la revisión `17603a4`, con el mismo host, 2 GiB de RAM asignada, sin swap del contenedor y dos hilos de inferencia:

| Configuración | Extracción | Embeddings | Pipeline, sin inicialización del modelo |
|---|---:|---:|---:|
| 1 CPU | 106,16 s | 47,68 s | 154,16 s |
| 3 CPU | 30,41 s | 19,80 s | 50,46 s |
| Código final `2065ac1`, 3 CPU, vistas diferidas | 30,30 s | 19,98 s | 50,66 s |

Ambas corridas produjeron 24 páginas, 144 chunks y 24 figuras, con la misma huella de integridad. La comparación no atribuye esa diferencia a una modificación de código: aquí solo cambió la cuota de CPU. La primera preparación del modelo también puede verse afectada por cachés del sistema.

La última fila corresponde al código Python final; cambios posteriores exclusivamente de documentación no alteran ese pipeline. Su huella de integridad también coincide con la base y conserva 24 páginas, 144 chunks y 24 figuras. El pico del contenedor fue aproximadamente 1,01 GiB, con cero swap en esa corrida.

La mejora observada de la configuración inicial (154,16 s) a la combinación final (50,66 s) es cercana a 3 veces, principalmente por la cuota de CPU. Comparando únicamente código bajo 3 CPU (50,46 frente a 50,66 s), este ensayo no demuestra una ganancia adicional. No se atribuye la aceleración a diferir previews ni a una caché de celdas. La primera apertura de una vista se paga aparte y se comprueba funcionalmente.

En el parser final, la detección/extracción de tablas consumió aproximadamente 26,09 s y las figuras 3,96 s. Esto sitúa el siguiente candidato de investigación en la detección de tablas/layout, conservando su precisión; no justifica desactivar indiscriminadamente análisis o imágenes. Son mediciones individuales del mismo corpus, no percentiles poblacionales ni una garantía para otros PDFs. La ejecución en otra máquina debe volver a medirse.

## Reproducción aislada (Linux / WSL)

Desde la raíz del repositorio, después de construir `aetherrag-app:local`:

```bash
mkdir -p output/benchmark
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --cpus 3 \
  --memory 2g --memory-swap 2g --pids-limit 128 \
  --tmpfs /tmp:rw,size=256m,mode=1777 \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$(pwd)/output/benchmark,target=/benchmark" \
  --entrypoint python aetherrag-app:local -u backend/scripts/benchmark_ingestion.py \
  --directory /benchmark --output /benchmark/after.json --pages 24 --defer-previews
```

Los pesos ya deben existir en la imagen. `--network none` impide llamadas de red; el proceso no conecta al catálogo, a Redis o a Qdrant. El único directorio del host montado es el de resultados y fuente sintética. No ejecutes el benchmark dentro del contenedor API/worker: duplicaría modelos dentro de su cuota de memoria.

Para comparar dos revisiones, construye imágenes con etiquetas diferentes y monta el mismo directorio de benchmark. Conserva CPU, RAM, versión de dependencias y número de hilos; cambia una variable por comparación. La revisión base necesita disponer del script de medición equivalente para instrumentar la misma secuencia de operaciones.

## Telemetría de los trabajos reales

`GET /api/v1/jobs/{job_id}` expone `queued_ms` y `timings_ms`. Los campos detallados se incorporan al finalizar el intento. Se guardan en PostgreSQL bajo el fencing de esa ejecución, sin migrar el esquema ni añadir texto privado al log de tiempos. Los intentos fallidos conservan el tiempo de la etapa que falló.

La medición `total_ms` cubre el trabajo hasta preparar/publicar su manifiesto; la duración de catálogo entre `started_at` y `finished_at` permite contrastar el total completo. Los subtiempos del parser están incluidos en `parse_ms` y no deben sumarse otra vez. La espera de inicialización anterior al claim se refleja en la espera del trabajo, no se atribuye al parser.

## Cambios y criterios de aceptación

1. Las imágenes de página completa se difieren; las figuras necesarias para la recuperación siguen extrayéndose en la ingesta.
2. La vista se genera en un proceso separado con límite de píxeles/deadline, caché y publicación atómica. Las pruebas comparan los PNG con el render anterior, incluidas rotaciones/CropBox y el rechazo de documentos no autorizados.
3. Se conserva la extracción nativa de celdas ya probada. Un prototipo de caché de texto no mostró una ganancia de tiempo en este corpus y no forma parte de la revisión final; las pruebas de decimales y geometría permanecen.
4. Los avances de embeddings se actualizan por lotes de ocho, conservando el tamaño interno del adaptador E5 y el orden de vectores.
5. El perfil de rendimiento reserva recursos con límites explícitos y un worker. La ampliación del presupuesto de extracción se realiza explícitamente, manteniendo límites de páginas, texto y píxeles.

La aceptación requiere huellas compatibles del corpus de comparación, pruebas de geometría/celdas, consultas demo con valores y figuras correctos, controles de sesión/CSRF y recorrido real por Celery. Una mejora de tiempo no compensa una regresión de contenido.
