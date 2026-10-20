# Seguridad

La revisión de código y las herramientas automáticas reducen riesgos; no garantizan ausencia de vulnerabilidades. Las dependencias y los servicios remotos cambian con el tiempo.

## Límites de confianza

- El manifiesto y el canal GitHub Releases son fuentes de código ejecutable. Usar únicamente un origen controlado y confiable. Una cuenta del proveedor comprometida puede distribuir código malicioso aunque las sumas coincidan.
- SHA-1 se exige para compatibilidad con el manifiesto del pack y detectar corrupción. No es una firma de autenticidad. El actualizador del launcher comprueba el SHA-256 publicado por GitHub para el ejecutable.
- HTTPS se exige para el manifiesto, sus mods y sus destinos finales. No se desactiva la validación de certificados.
- Los nombres de mods no pueden contener rutas, unidades, flujos NTFS ni nombres reservados de Windows. Se valida todo el manifiesto antes de modificar mods.
- Las descargas se verifican en archivos temporales exclusivos antes de reemplazarse. No se eliminan mods obsoletos si falla una descarga.
- Hay límites de tamaño para manifiestos, mods y respuestas de estado Minecraft, y límites para VarInts recibidos.
- El launcher no eleva privilegios. Ejecutarlo como usuario normal, nunca como administrador.
- La conexión offline por nick no demuestra identidad de una cuenta Microsoft. El servidor debe mantener su autenticación y permisos; el launcher no sustituye ese control.

No subir configuraciones de usuarios, mundos, registros de conexión ni claves. Reportar fallos de manera privada al propietario del repositorio sin adjuntar secretos ni datos de jugadores.

## Revisión previa a publicación — 20 de octubre de 2026

- Exportación nueva, sin historial del directorio de trabajo original: sólo código, assets, pruebas y documentación.
- Se retiraron metadatos de texto de los PNG conservando sus píxeles. No se incluyeron capturas de sesiones ni perfiles del usuario.
- Gitleaks 8.30.1: sin secretos detectados en los archivos candidatos.
- Bandit 1.9.4: sin hallazgos de severidad media o alta pendientes. Se revisaron sus avisos bajos relativos al uso esperado de subprocess, tokens offline vacíos y recuperación de errores de instalación.
- pip-audit 2.10.1 sobre las dependencias de ejecución: sin vulnerabilidades conocidas tras actualizar Pillow a 12.3.0.
- Seis pruebas de seguridad pasan: rutas y nombres Windows, HTTPS/checksums, duplicados, rechazo de caché inválida, instalación verificada y respuestas de red malformadas. El manifiesto real del pack y las importaciones del launcher también se comprobaron.

Los endpoints del proyecto (repositorio de distribución y servidor predeterminado) son configuración de servicio y permanecen en el código; no son credenciales de acceso. Esta revisión no es una garantía de seguridad futura ni una prueba de penetración independiente.
