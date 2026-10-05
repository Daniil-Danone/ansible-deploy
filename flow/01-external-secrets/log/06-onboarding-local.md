# Шаг 6 · Локально проверенный onboarding

- Packaged versioned scaffold создаёт commit-safe Stage/Prod/Monitoring/Restore files.
- `project sync` обновляет только untouched templates и выдаёт conflict candidate для
  user-modified config/Compose.
- Canonical runbook использует schema v2 и внешний project-scoped secret store; OS
  guides содержат только платформенные различия.
- Reusable CI contract материализует внешний store в runner temp и удаляет его через
  `always()` cleanup; PR не выполняет deploy, Production использует Environment gate.

Доказательства: `tests/test_project_sync.py`, `tests/test_ci_contract.py`,
`tests/test_docs_contract.py`. Live VPS/DNS, GitHub required reviewers и Google Drive
не проверялись без инфраструктуры заказчика и остаются manual acceptance.
