# CLI

Глобальные options ставятся перед командой:

```text
deploy [--project-dir PATH] [--verbose] COMMAND ...
```

Основные команды:

```text
deploy project init
deploy project sync [--check]
deploy secrets path [--new-project-id]
deploy images publish <stage|prod> ...
deploy stage [--dry-run] [--version SHA]
deploy prod [--dry-run] [--version SHA] [--yes]
deploy status <stage|prod>
deploy rollback prod [--yes]
deploy server update <stage|prod|monitoring|all> [--dry-run] [--yes]
deploy monitoring <deploy|status|update> [--dry-run]
deploy collectors <deploy|status|update> <stage|prod|all> [--dry-run]
deploy backup setup prod
deploy backup run prod
deploy backup list prod
deploy backup restore prod --target restore --backup ID [--yes]
```

`project sync --check` ничего не пишет и возвращает ненулевой код при pending update
или conflict. Обычный sync никогда не перезаписывает изменённый config/Compose.

`--dry-run` применяет Ansible check mode там, где он безопасен, и не выполняет
bootstrap с password. Production-changing commands требуют интерактивного `prod`; в
non-interactive protected CI используется `--yes`.

Код `0` означает доказанный успех команды, `2` — configuration/security boundary;
runner возвращает ненулевой код underlying operation. Не анализируйте только текст:
automation должна проверять exit code.
