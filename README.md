# Barbara GitHub Actions sample

Este repositorio deja un ejemplo mínimo de despliegue continuo hacia Barbara desde GitHub Actions.

## Flujo propuesto

1. Un tag `v*` o una ejecución manual dispara el workflow.
2. Se construye un ZIP con:
   - el código de `app/`
   - `Dockerfile`
   - `docker-compose.yaml`
3. Se crea la aplicación en Barbara si todavía no existe.
4. Se publica una nueva versión con el ZIP generado.
5. Se despliega esa versión en los dispositivos listados en `deployment/devices.json`.
6. Se consulta la salud de la app en cada dispositivo.
7. Si la app no queda sana, se relanza la versión previa en ese dispositivo.

## Secretos de GitHub necesarios

- `BARBARA_API_BASE_URL`
- `BARBARA_API_TOKEN`

## Qué hay que adaptar a la API real

El archivo `scripts/barbara_pipeline.py` asume estos endpoints REST:

- `GET /applications?name=...`
- `POST /applications`
- `POST /applications/{application_id}/versions`
- `GET /devices/{device_id}/applications/{application_id}`
- `POST /deployments`
- `GET /devices/{device_id}/applications/{application_id}/health`

También asume respuestas JSON con estas claves:

- aplicación: `id`, `name`
- versión: `id`, `version`
- estado actual en dispositivo: `versionId`
- salud: `status`, `version`

Si Barbara usa otros nombres o rutas, solo hay que ajustar esa clase cliente.

## Decisión tomada en este ejemplo

He dejado el despliegue automático sobre tags `v*` porque normalmente es más seguro que desplegar cada commit. Si más adelante quieres, lo cambiamos a:

- despliegue en cada push a una rama concreta
- despliegue al crear una release
- despliegue manual con aprobación

## Ejecución local

```bash
python scripts/barbara_pipeline.py \
  --app-name sample-barbara-app \
  --release-version v1.0.0 \
  --devices-file deployment/devices.json \
  --source-dir app \
  --dockerfile Dockerfile \
  --compose-file docker-compose.yaml
```

## Siguiente paso recomendado

Con la documentación real de Barbara, sustituimos los endpoints y payloads asumidos por los definitivos y dejamos el ejemplo listo para ejecutar de verdad.
