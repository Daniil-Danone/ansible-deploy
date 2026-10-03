# Структура проекта

```text
my-app/
├── .deploy/
│   ├── config/global.yml
│   ├── images.yml
│   ├── keys/                         # ignored
│   └── environments/
│       ├── stage/
│       │   ├── config.yml
│       │   ├── app.env               # ignored
│       │   └── registry-auth.json     # ignored
│       └── prod/...
├── .deploy-state/                    # ignored, inventory/known_hosts
├── deploy/
│   ├── compose.stage.yml
│   └── compose.prod.yml
├── backend/
└── frontend/
```

Добавьте в `.gitignore`:

```gitignore
.deploy-state/
.deploy/keys/
.deploy/environments/*/app.env
.deploy/environments/*/registry-auth.json
*.ansible-deploy.lock
```

`.deploy/` не может быть symlink. Relative application paths должны оставаться внутри
project root. Secret registry auth обязан находиться внутри проекта и не может совпадать
с Compose, env, keys или configs. Lock-файлы publisher можно оставить ignored.

Legacy `config/` + `environments/` в корне распознаются только ради совместимости;
новые проекты должны использовать `.deploy/`.
