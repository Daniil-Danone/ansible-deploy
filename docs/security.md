# Безопасность

- `.deploy/` commit-safe; реальные secrets и private keys находятся только во внешнем
  project-scoped store из `deploy secrets path`.
- Не передавайте secrets через arguments, Compose YAML, GitHub artifacts, job summary
  или `set -x`. Base64 — encoding, не encryption.
- В Compose `environment` допустимы ссылки на переменные внешнего env-файла, включая
  переменные с secret-like именами (`POSTGRES_PASSWORD: ${DB_PASSWORD}`): в commit
  попадает только имя. Запрещены литеральные secrets, значения по умолчанию у
  интерполяций (`${NAME:-value}`) и встроенные credentials.
- Schema v2 ограничивает sensitive paths внешним root и проверяет type, owner,
  permissions, hardlink и symlink/reparse traversal непосредственно перед use.
- Для Stage, Production, Monitoring и Restore используйте разные SSH/registry/runtime
  credentials. Registry pull token должен быть read-only.
- SSH host fingerprints записывает `ansible-deploy trust <environment>`; сверяйте их с
  консолью провайдера и коммитьте осознанно. Смена уже доверенного ключа требует
  явного `--force`. Private key не копируется на server; bootstrap password не
  сохраняется.
- Production images закрепляются digest; deployment version — полный/допустимый Git
  SHA. Production operation требует confirmation или protected CI Environment.
- Backup encrypted age identity и rclone credentials находятся во внешнем store;
  Restore Drill всегда использует отдельный target.
- В GitHub Actions выдавайте минимальные `contents: read`/`packages: read`, pin tool
  полным SHA, храните environment secrets отдельно и всегда удаляйте runner temp store.

Перед commit:

```bash
git status --short
git diff --check
git grep -n -E 'BEGIN .*PRIVATE KEY|registry-auth|GF_SECURITY_ADMIN_PASSWORD='
```

Последний grep — эвристика, а не гарантия. При утечке удаление файла новым commit
недостаточно: отзовите credential, очистите историю согласованным способом и выдайте
новый secret.
