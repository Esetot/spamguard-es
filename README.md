# SpamGuard ES

Filtro anti-spam Android con backend de reputación autónomo en GitHub.

## Arquitectura

### 1. Backend GitHub

`backend/spam_reputation_scraper.py` consulta las fuentes, mantiene histórico y
clasifica teléfonos como `BLOCK`, `REVIEW`, `OBSERVE` o `ALLOW`.

`.github/workflows/update-spam.yml` se ejecuta diariamente a las **18:17
Europe/Madrid**, actualiza `data/` y hace `commit + push` automáticamente.

La app móvil consume únicamente:

- `data/lista_numeros_spam.txt` — BLOCK
- `data/lista_numeros_review.txt` — REVIEW
- `data/mobile_manifest.json` — hashes SHA-256 y metadatos

### 2. Android / Buildozer

`android_app/` contiene Kivy + Buildozer y una capa Java nativa.

- `SpamCallScreeningService.java`: decide la llamada localmente.
- `SpamGuardSyncJobService.java`: sincronización nativa periódica con GitHub.
- `SpamGuardUpdateScheduler.java`: programa la sincronización cada 12 h.
- `SpamGuardRoleHelper.java`: solicita `ROLE_CALL_SCREENING`.

La aplicación hace además una sincronización al abrirse.

## Política en llamada

- `BLOCK` -> bloquea/rechaza la llamada.
- `REVIEW` -> la silencia.
- no listado -> permite.
- error interno -> **fail open**, permite.

La decisión nunca consulta Internet y no depende de que Kivy/Python esté abierto.

## Privacidad

Sólo se solicitan:

- `INTERNET` para actualizar la base.
- `RECEIVE_BOOT_COMPLETED` para conservar el JobScheduler tras reinicio.

No solicita `READ_CONTACTS`, `READ_CALL_LOG` ni `READ_PHONE_STATE`.
Por ello Android no entrega al filtro llamadas de números guardados en contactos.

## Instalación rápida usando GitHub

1. Crea un repositorio GitHub **público**.
2. Sube el contenido completo de este ZIP a la raíz.
3. Abre **Actions -> Update spam reputation -> Run workflow**.
4. Cuando termine, abre **Actions -> Build Android APK -> Run workflow**.
5. Descarga el artifact `SpamGuard-ES-debug`.
6. Instala el APK en Android.
7. Abre SpamGuard ES y pulsa **Activar filtro de llamadas**.
8. Android te pedirá confirmar que SpamGuard ES sea la app de filtrado.

El workflow de compilación inserta automáticamente la URL RAW de tu propio
repositorio dentro del APK; no tienes que editarla manualmente.

## Compilar en Windows

Buildozer Android se usa desde Linux. Para Windows se incluye `BUILD_WSL2.md`.
