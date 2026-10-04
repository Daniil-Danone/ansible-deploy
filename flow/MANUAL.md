# Ручные действия

Что владельцу нужно сделать руками, чтобы система заработала и защиты
действовали. Правила — `.codex/guidelines/reporting.md`. `/go` дописывает
пункты своего шага, `/ship` сверяет открытые.

**Инструкция (артефакт):** — ещё не опубликована —

Статусы: `открыт` · `сделан` (владелец подтвердил или агент проверил — чем) ·
`решение` (ждёт выбора владельца) · `позже` (нужно к шагу или итерации N).

| # | Что сделать | Откуда | Когда нужно | Статус |
|---|---|---|---|---|
| M1 | Подготовить 4 Ubuntu 24.04 VPS: Stage, Production, Monitoring, Restore Drill | финальная приёмка | итерация 4 | позже |
| M2 | Создать DNS A/AAAA для Stage, Production и Grafana; Restore — опционально | финальная приёмка | итерация 4 | позже |
| M3 | Подготовить Google Drive service account/OAuth credential и folder/shared-drive ID | Backup/Restore | итерация 2 | позже |
| M4 | Настроить GitHub Environments `stage` и `production`, required reviewer для Production | CI/CD | итерация 3 | позже |
| M5 | Выбрать application repo/trigger contract для immutable image | CI/CD | итерация 3 | решение |
| M6 | Подготовить PostgreSQL acceptance fixture с marker row и marker file | Backup/Restore | итерация 2 | позже |
