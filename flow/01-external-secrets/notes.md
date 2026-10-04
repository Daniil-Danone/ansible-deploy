# Заметки · итерация 1

## Шаг 2 · schema v2

**Сюрприз.** Перевод default demo-конфигурации на schema v2 нельзя коммитить
отдельно от writer-ов шага 4: `images publish` всё ещё требует
`registry_auth_file` внутри project root и падает на внешнем пути. Тесты это
скрывали, принудительно понижая copied demo до schema v1.

**Грабли.** Path validation при config load и повторная validation в runner не
закрывают окно между закрытием handles и `Popen`, если родитель external root
доступен другому локальному пользователю. Контракт должен включать trusted
owner-only ancestry до root; same-user процесс остаётся вне threat model,
поскольку уже может читать секреты.

**Грабли.** Redaction missing-file недостаточна: reader/parser исключения могут
включить абсолютный путь или содержимое. User-facing ConfigurationError для
secret operations должен разрывать exception chain (`from None`) и использовать
только logical field/environment.

**Решение.** Владелец 2026-10-05 подтвердил объединение шагов 2 и 4: schema
v2, default demo, key/registry writers и runner use-boundary становятся одним
атомарным контрактом. Промежуточное состояние, где default config уже v2, а
writers поддерживают только project-local paths, не коммитится.

**Грабли.** После двух fix/review циклов объединённого шага остались дефекты
на публичной границе: `ensure_deploy_key` маскирует rollback failure внутреннего
writer, два permission validator сохраняют sensitive `__cause__`, а Windows
key lock меняет ACL до фактического lock и редко падает при конкурентном
создании. Зелёный полный pytest не заменяет targeted fault/concurrency tests.
По правилу двух review-кругов дальнейшая стабилизация требует явного
подтверждения владельца.

**Решение.** Владелец 2026-10-05 разрешил дополнительный stabilization-pass:
исправить public rollback reporting, полную exception-chain redaction и
Windows lock race, расширить fault/concurrency regressions и выполнить свежее
независимое ревью перед закрытием шага.

**Решение.** Документация должна содержать единый deployment runbook с картой
ролей Stage/Prod/Monitoring/Collectors, матрицей команд, порядком первого
bootstrap и обычных операций, проверками и диагностикой. Runbook оформляется
отдельным PR после external secret storage, чтобы сразу описывать финальный
secret layout, а не временный `.deploy` layout.
