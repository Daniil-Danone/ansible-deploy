# Итерация 1 · External secret storage

- [x] 1. Контракт project identity и внешнего secret root
      Готово: T1–T4 зелёные; `deploy secrets path` печатает только внешний путь
- [~] 2. Schema v2: все чувствительные пути резолвятся только внутри secret root
      Готово: T5–T9 зелёные; `.deploy/` не является допустимым root для реального секрета
- [ ] 3. Создание и аудит secret store
      Готово: T10–T13 зелёные; `deploy secrets init` повторяем; `deploy secrets audit` обнаруживает permissions и worktree leaks
- [ ] 4. Key generation и registry auth используют внешний store
      Готово: T14–T16 зелёные; runner монтирует только отдельные secret files read-only
- [ ] 5. Fail-closed миграция legacy project-local secrets
      Готово: T17–T21 зелёные; collision/partial failure не меняют источник и config
- [ ] 6. Onboarding, CI contract и читаемая документация
      Готово: T22–T24 зелёные; clean-copy walkthrough не создаёт secrets в Git worktree
- [ ] 7. Независимая security-проверка итерации
      Готово: полный набор из `flow/PROJECT.md` зелёный; reviewer не оставил blocker/major; `python .codex/tools/verify.py` не нашёл утечек
- [ ] 8. `/ship`: PR external secret storage
      Готово: PR в `develop` зелёный и смержен; ветка удалена; локальный `develop` актуален

## Заходы

- 1–2 · единый контракт identity, root и schema v2
- 3–5 · lifecycle secret store, writers и атомарная миграция
- 6–7 · onboarding и независимая проверка
- 8 · закрытие итерации и PR

## Сверка по опасностям

- hz-1: неприменимо — денежных/числовых расчётов нет.
- hz-2: T10, T17, T18, T20 проверяют повторный запуск и атомарность миграции.
- hz-3: T12 и T20 проверяют collision/exclusive creation; распределённого состояния нет.
- hz-4: T17–T21 закрывают переход schema v1 → v2 и повторную миграцию.
- hz-5: T3, T6–T9 закрывают path traversal, user-controlled paths и fail-closed input.
- hz-6: T21 ограничивает объём и типы мигрируемых файлов; обход всего диска запрещён.
- hz-7: T4, T9, T13, T16, T23 закрывают disclosure в errors/argv/env/logs/Git.
- hz-8: неприменимо — отдельного UI нет.
- Шов hz-2/hz-3: T20 — collision в середине миграции оставляет source/config неизменными.
- Шов hz-6/hz-7: T13 — audit сообщает путь/тип нарушения, но не значение секрета.
