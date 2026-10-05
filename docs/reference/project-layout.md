# Структура application repository

```text
application/
├── .deploy/
│   ├── project-id                 # committed stable UUID
│   ├── template-state.yml         # scaffold version + hashes
│   ├── config/global.yml
│   ├── images.yml
│   └── environments/
│       ├── stage/config.yml
│       ├── prod/config.yml
│       ├── monitoring/config.yml
│       └── restore/config.yml
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
namespace. Пример логической структуры root:

```text
<external-root>/
├── environments/{stage,prod,monitoring,restore}/...
├── keys/{stage,prod,monitoring,restore}_ed25519
└── backup/{rclone.conf,age.key}
```

`.deploy-state/` — локальное несекретное execution state/history, оно не коммитится.
Упакованный Ansible runtime принадлежит установленному CLI, а не копируется в каждое
application repository.
