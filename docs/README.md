# Документация

Начинайте с [канонического runbook](runbook.md): это единственная полная
последовательность от scaffold до проверки Production. ОС-инструкции содержат только
установку и синтаксис своей платформы:

- [Windows](getting-started/windows.md)
- [macOS](getting-started/macos.md)
- [Ubuntu](getting-started/ubuntu.md)

## Эксплуатация

- [Backup, Restore Drill и disaster recovery](guides/backup-restore.md)
- [GitHub Actions CI/CD](guides/ci-cd.md)
- [Обновление CLI и project sync](guides/upgrading.md)
- [Production](guides/production.md)
- [Monitoring и collectors](guides/monitoring.md)
- [Приватные registry](guides/private-registries.md)
- [Диагностика и восстановление после ошибки](troubleshooting.md)

## Справочник и модель системы

- [Команды CLI](reference/cli.md)
- [Конфигурация schema v2](reference/configuration.md)
- [Структура application repository](reference/project-layout.md)
- [Как работает deploy](concepts/how-it-works.md)
- [SSH и ключи](concepts/ssh-and-keys.md)
- [Управляемое состояние сервера](concepts/server-state.md)
- [Security boundaries](security.md)

## Статус доказательств

Unit/contract/integration-проверки подтверждают парсинг конфигурации, защиту путей,
redaction, scaffold sync, Ansible syntax и структуру workflows. Проверки на реальных
VPS, DNS, GitHub Environment approval и Google Drive выполняются вручную с
инфраструктурой заказчика; зелёные локальные тесты не заменяют эти проверки.
