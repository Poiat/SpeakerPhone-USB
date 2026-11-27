## Resumen

Describe el problema y la solución en 2–4 líneas.

## Tipo de cambio

- [ ] Corrección de bug
- [ ] Nueva funcionalidad
- [ ] Refactor / mantenimiento
- [ ] Documentación / CI
- [ ] Cambio de protocolo (PC + Android)

## Verificación

- [ ] `python -m pytest -q`
- [ ] `ruff check --select E9,F ...`
- [ ] `python scripts/check_version.py`
- [ ] Probado en Linux
- [ ] Probado en Windows
- [ ] Probado en macOS
- [ ] Probado en Android

Plataformas/dispositivos usados:

## Checklist

- [ ] No incluí artefactos generados (`build/`, `dist/`, `.gradle/`, `local.properties`, AppImages).
- [ ] Actualicé versión, `CHANGELOG.md` y notas del release si aplica.
- [ ] Si cambié el protocolo, actualicé PC y Android y lo documenté.
- [ ] Añadí o actualicé tests para la lógica nueva.
- [ ] Revisé que no se filtren credenciales ni datos personales en logs.
