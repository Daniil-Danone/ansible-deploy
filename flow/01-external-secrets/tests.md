# Тесты · итерация 1

| # | Сценарий — поведение | Уровень | Тест | Шаг | |
|---|---|---|---|---|---|
| T1 | Один committed project-id даёт стабильный namespace после move/clone | unit ! | `tests/test_secret_store.py::test_t1_committed_project_id_keeps_namespace_after_move_and_clone` | 1 | [x] |
| T2 | `--new-project-id` создаёт другой namespace без копирования секретов | unit ! | `tests/test_secret_store.py::test_t2_new_project_id_changes_namespace_without_copying_secrets` | 1 | [x] |
| T3 | Missing/invalid/symlinked project-id отклоняется fail closed | unit ! | `tests/test_secret_store.py::test_t3_*`, adversarial read/write tests | 1 | [x] |
| T4 | Windows, Linux, macOS и env override возвращают внешний Unicode/space-safe path без secret values | unit ! | `tests/test_secret_store.py::test_t4_*` | 1 | [x] |
| T5 | app env, registry auth, collector password, monitoring env и SSH private key резолвятся внутри external root | unit ! | `tests/test_external_secret_schema.py::test_t5_*` | 2 | [x] |
| T6 | Absolute path, `..` и пустой sensitive path отклоняются | unit ! | `tests/test_external_secret_schema.py::test_t6_*`, portable-name matrix | 2 | [x] |
| T7 | Symlink/junction/reparse escape из secret root отклоняется | unit ! | `tests/test_external_secret_schema.py::test_t7_*`, launch-boundary swap tests | 2 | [x] |
| T8 | Secret file обязан быть regular, owner-only и без hardlink alias | unit ! | `tests/test_external_secret_schema.py::test_t8_*`, POSIX/Windows ACL tests | 2 | [x] |
| T9 | Validation error не содержит sensitive input/value | unit ! | `tests/test_external_secret_schema.py::*suppresses*`, shared-boundary fault matrix | 2 | [x] |
| T10 | `secrets init` создаёт root 0700/owner-only и templates 0600 без реальных значений | integration ! | | 3 | [ ] |
| T11 | Повторный `secrets init` не перезаписывает пользовательские файлы | integration ! | | 3 | [ ] |
| T12 | Одновременное/existing-file создание не приводит к overwrite | integration ! | | 3 | [ ] |
| T13 | `secrets audit` находит tracked/known secret в worktree и unsafe ACL, не печатая содержимое | integration ! | | 3 | [ ] |
| T14 | Key generation создаёт пару только во внешнем store и не перезаписывает её | integration ! | `tests/test_external_secret_schema.py::test_t14_*`, `tests/test_keys.py::*rollback*` | 2 | [x] |
| T15 | Registry publish создаёт auth только во внешнем store и rollback сохраняет прежний файл | integration ! | `tests/test_external_secret_schema.py::test_t15_*`, `tests/test_images.py::*rollback*` | 2 | [x] |
| T16 | Runner монтирует только отдельные external secret files `:ro`; content отсутствует в argv/env | unit ! | `tests/test_external_secret_schema.py::test_t16_*`, runner boundary fault matrix | 2 | [x] |
| T17 | Legacy v1 layout мигрируется в schema v2 с byte equality и restrictive permissions | integration ! | | 5 | [ ] |
| T18 | Повторная migration идемпотентна | integration ! | | 5 | [ ] |
| T19 | Destination collision запрещает overwrite и оставляет source/config неизменными | integration ! | | 5 | [ ] |
| T20 | Ошибка посередине migration атомарно откатывает destination/config | integration ! | | 5 | [ ] |
| T21 | Tracked legacy source требует rotation warning и никогда не удаляется автоматически | integration ! | | 5 | [ ] |
| T22 | Demo copy + init оставляет Git worktree без известных real-secret filenames | integration ! | `tests/test_project_sync.py::test_scaffold_contains_no_runtime_secret_files_or_values` | 6 | [x] |
| T23 | CI создаёт store под runner temp без echo и удаляет его в `always()` cleanup | contract ! | `tests/test_ci_contract.py` | 6 | [x] |
| T24 | README ведёт по одному пути install → init → secrets → deploy, команды проходят smoke parsing | contract | `tests/test_docs_contract.py`, `tests/test_project_sync.py` | 6 | [x] |

`!` требует красного прогона на временно отключённой защите и зелёного после
восстановления защиты; оба вывода фиксируются в `flow/01-external-secrets/log/`.
