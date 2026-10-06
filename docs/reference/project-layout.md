# Структура application repository

```text
application/
├── .deploy/
│   ├── project-id                 # committed stable UUID
│   ├── template-state.yml         # scaffold version + hashes
│   ├── config/global.yml
│   ├── images.yml
│   ├── cd.yml                     # project-owned CD branches + immutable CLI SHA
│   └── environments/
│       ├── stage/config.yml
│       ├── prod/config.yml
│       ├── monitoring/config.yml
│       └── restore/config.yml
├── .github/workflows/deploy.yml   # managed GitHub Actions caller
├── deploy/
│   ├── compose.stage.yml
│   ├── compose.prod.yml
│   └── compose.restore.yml
└── application source
```

Все перечисленное commit-safe. `.env.example` может содержать только имена и
нерабочие placeholders. Запрещены реальные `app.env`, private keys,
`registry-auth.json`, collector passwords, Grafana/Loki credentials, rclone config и
age identity.

Внешний store определяется committed `.deploy/project-id`, поэтому clone/move не меняет
namespace. Каталоги и заготовки secret-файлов создаёт `ansible-deploy secrets init`.
Пример логической структуры root:

```text
<external-root>/
├── environments/{stage,prod,monitoring,restore}/...
├── keys/{stage,prod,monitoring,restore}_ed25519
└── backup/{rclone.conf,age.key}
```

Например, для Stage:

```text
<external-root>/environments/stage/
├── app.env                # application.env_file
├── bot.env                # application.extra_env_files[].source (optional)
├── registry-auth.json
└── collector.password
```

`.deploy-state/` — локальное несекретное execution state/history, оно не коммитится.
Упакованный Ansible runtime принадлежит установленному CLI, а не копируется в каждое
application repository.
