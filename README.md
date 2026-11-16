# Fuente AltStore de YTKACE

Mantiene una fuente pública de AltStore con la versión actual de las dos variantes oficiales de YTKACE: iOS 16 y iOS 17 o posterior. Las IPA originales se conservan sin modificar como assets del Release estable `ytkace-current`; no se incorporan al historial Git.

## Añadir a AltStore

Añade esta URL como fuente en AltStore:

**https://raw.githubusercontent.com/dfernandezdaw/ytkace-altstore/main/source.json**

Las versiones, builds, requisito mínimo de iOS, tamaño, entitlements firmados y descripciones de privacidad se extraen de las IPA oficiales. La sincronización valida ambas variantes, el Bundle Identifier común y los metadatos requeridos; si no puede extraerlos o validarlos, el job falla. El nombre de la app, desarrollador, descripción, icono, URLs y estructura de la fuente son configuración explícita del repositorio, no datos inferidos del paquete. El orden muestra primero la variante iOS 17+ y luego la compatible desde iOS 16, para que cada dispositivo reciba la opción adecuada.

## Sincronización

GitHub Actions consulta el último Release publicado de `itzzace/ytkace`, identifica sus assets IPA por el indicador de variante iOS 16 o iOS 17+ y falla de forma segura si falta alguna variante o la identificación es ambigua. Descarga los paquetes temporales, extrae `Info.plist`, el perfil de aprovisionamiento si lo hay y datos de tamaño/SHA-256, valida que ambas variantes compartan Bundle Identifier y publica los bytes originales con nombres estables `YTKACE-iOS16.ipa` y `YTKACE-iOS17.ipa` en el Release `ytkace-current`.

El workflow se ejecuta automáticamente cada seis horas y también puede iniciarse manualmente desde **Actions → Sync YTKACE AltStore source → Run workflow**. Usa el `GITHUB_TOKEN` automático y el permiso `contents: write`. `source.json` se confirma en `main` únicamente cuando cambia. El Release estable mantiene solo los dos assets IPA actuales.

Las versiones, builds, requisito mínimo de iOS declarado en `Info.plist`, tamaño, entitlements firmados (extraídos con `codesign` del ejecutable de la app y sus extensiones) y descripciones de privacidad de los `Info.plist` proceden de los paquetes. Las declaraciones de permisos se combinan para ambas variantes en `appPermissions`, como exige AltStore. El job usa macOS para leer entitlements firmados; si falta la firma o no se pueden extraer, falla en vez de inventarlos. Los SHA-256 se comparan para evitar reemplazos innecesarios. La sincronización no modifica ni vuelve a firmar los paquetes.

## Variantes

La inspección de la Release upstream v1.1.8 confirmó `YTKACE_1.1.8_YouTube_iOS16_21.33.6.ipa` para iOS 16 y `YTKACE_1.1.8_YouTube_21.46.3.ipa` para iOS 17+. El sincronizador decide a partir de los nombres detectados en cada último Release, no codifica esos nombres upstream.

## Errores

Consulta **Actions**, abre la ejecución fallida y revisa el paso **Synchronize upstream release**. La ejecución falla si no hay exactamente una IPA reconocible por variante, si los paquetes no contienen un único `Info.plist` de aplicación, faltan los metadatos de identidad/versión, los tamaños no coinciden o los Bundle Identifier difieren. Para errores de permisos de publicación, revisa que el workflow pueda usar su token con `contents: write`.
