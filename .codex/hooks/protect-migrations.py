#!/usr/bin/env python3
"""
protect-migrations — миграции пишет агент, применяет владелец.

Правило существует у всех, кто дорожит данными, и словами оно не держится:
в двух разных сессиях агент прогнал `alembic upgrade` против базы, потому что
«это же одноразовый контейнер». Проверить, одноразовая база или боевая, из
команды нельзя, а цена ошибки необратима. Поэтому применение миграций
блокируется механически.

Что разрешено: создать миграцию, посмотреть историю, проверить согласованность
моделей и схемы (`revision`, `check`, `history`, `heads`, `current`, `show`,
`makemigrations`, `diff`). Что запрещено: всё, что меняет схему живой базы.

Режим:
  python protect-migrations.py --pre-tool   хук PreToolUse(Bash|PowerShell)

Хук читает JSON на stdin и возвращает 2, чтобы заблокировать вызов.
Только stdlib.
"""

import json
import re
import shlex
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# (как узнать инструмент, применяющие подкоманды)
TOOLS = [
    (lambda w: w == "alembic", {"upgrade", "downgrade", "stamp"}),
    (lambda w: w.endswith("manage.py"), {"migrate", "flush", "sqlflush"}),
    (lambda w: w == "prisma", {"deploy", "reset", "dev"}),          # prisma migrate <sub>
    (lambda w: w == "dbmate", {"up", "down", "rollback", "migrate", "drop"}),
    (lambda w: w == "goose", {"up", "down", "reset", "redo", "up-to", "down-to"}),
    (lambda w: w == "flyway", {"migrate", "clean", "undo", "baseline", "repair"}),
    (lambda w: w == "liquibase", {"update", "rollback", "dropAll"}),
    (lambda w: w == "atlas", {"apply"}),                            # atlas migrate apply
    (lambda w: w == "rake" or w == "rails", {"db:migrate", "db:rollback", "db:schema:load", "db:reset", "db:drop"}),
    (lambda w: w == "knex", {"migrate:latest", "migrate:rollback", "migrate:down", "migrate:up"}),
]

# Слова-обёртки, за которыми идёт настоящая команда: uv run alembic …,
# docker compose run --rm api alembic …, poetry run python manage.py migrate.
SKIP = {
    "sudo", "env", "time", "nohup", "xargs",
    "uv", "uvx", "poetry", "pipenv", "pdm", "hatch", "rye", "npx", "pnpm", "yarn", "bun", "bundle",
    "run", "exec", "python", "python3", "py", "node", "ruby",
    "docker", "compose", "docker-compose", "podman",
}

MIGRATION_SERVICES = {"migrate", "migration", "migrations"}
COMPOSE_ACTIONS = {"up", "run", "start"}
COMPOSE_VALUE_OPTIONS = {
    "-f", "--file", "-p", "--project-name", "--profile", "--env-file",
    "--project-directory", "--parallel", "--ansi", "--progress",
}
ACTION_VALUE_OPTIONS = {
    "--scale", "--timeout", "--wait-timeout", "-e", "--env", "-v", "--volume",
    "-w", "--workdir", "-u", "--user", "--name", "--entrypoint", "--cap-add", "--cap-drop",
}


def _skip_options(words, index, value_options):
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        index += 1
        if "=" not in option and option in value_options and index < len(words):
            index += 1
    return index


def _token(word):
    return word.strip("\"'").casefold()


def _executable(word):
    name = word.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(?:exe|cmd|bat)$", "", name, flags=re.I).casefold()


def compose_invocation(words):
    """(action, services, no_deps, compose_index) либо None."""
    lowered = [_token(w) for w in words]
    compose_at = None
    for i, word in enumerate(words):
        executable = _executable(word)
        if executable == "docker-compose":
            compose_at = i + 1
            break
        if executable == "docker":
            try:
                compose_at = lowered.index("compose", i + 1) + 1
            except ValueError:
                pass
            break
    if compose_at is None:
        return None
    i = _skip_options(lowered, compose_at, COMPOSE_VALUE_OPTIONS)
    if i >= len(lowered) or lowered[i] not in COMPOSE_ACTIONS:
        return None
    action = lowered[i]
    args = lowered[i + 1:]
    no_deps = "--no-deps" in args
    first = _skip_options(lowered, i + 1, ACTION_VALUE_OPTIONS)
    services = lowered[first:first + 1] if action == "run" else [w for w in lowered[first:] if not w.startswith("-")]
    return action, services, no_deps, compose_at


def _compose_files(words, compose_at, cwd):
    files = []
    i = compose_at
    while i < len(words):
        token = _token(words[i])
        if token in COMPOSE_ACTIONS:
            break
        if token in ("-f", "--file") and i + 1 < len(words):
            files.append(words[i + 1].strip("\"'"))
            i += 2
            continue
        if token.startswith("--file="):
            files.append(words[i].split("=", 1)[1].strip("\"'"))
        i += 1
    if not files:
        files = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"]
    result = []
    for raw in files:
        path = Path(raw)
        result.append(path if path.is_absolute() else cwd / path)
    return result


def _declared_services(path):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    services_indent = None
    child_indent = None
    services = set()
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if services_indent is None:
            if re.match(r"^\s*services\s*:\s*(?:#.*)?$", line):
                services_indent = indent
            continue
        if indent <= services_indent:
            break
        match = re.match(r"^\s*([\"']?[^\s:#\"']+[\"']?)\s*:\s*(?:#.*)?$", line)
        if not match:
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            services.add(match.group(1).strip("\"'").casefold())
    return services


def compose_declares_migration(words, compose_at, cwd):
    return any(
        _declared_services(path) & MIGRATION_SERVICES
        for path in _compose_files(words, compose_at, cwd)
        if path.is_file()
    )


def offending(cmd: str, cwd: Path):
    """Вернуть (инструмент, подкоманда), если команда применяет миграции."""
    for seg in re.split(r"[|;&\n]+", cmd or ""):
        seg = seg.strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg, posix=False)
        except ValueError:
            words = seg.split()
        invocation = compose_invocation(words)
        if invocation:
            action, services, no_deps, compose_at = invocation
            service = next((s for s in services if s in MIGRATION_SERVICES), None)
            if service:
                return "docker compose", service
            if compose_declares_migration(words, compose_at, cwd) and not (no_deps and services):
                return "docker compose", f"{action} (может запустить migration dependency)"
        # Остаются только «слова»: без опций и без присваиваний вида VAR=1
        clean = [w for w in words
                 if not w.startswith("-") and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        for i, w in enumerate(clean):
            name = _executable(w)
            for is_tool, subs in TOOLS:
                following = [_token(nxt) for nxt in clean[i + 1:i + 4]]
                if is_tool(name) and any(nxt in subs for nxt in following):
                    return name, next(nxt for nxt in following if nxt in subs)
    return None


def main() -> int:
    if "--pre-tool" not in sys.argv:
        sys.stderr.write("protect-migrations: ожидается hook event режима --pre-tool; вызов заблокирован.\n")
        return 2
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.stderr.write("protect-migrations: некорректный или пустой hook event; вызов заблокирован.\n")
        return 2
    if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str) or not data.get("tool_name"):
        sys.stderr.write("protect-migrations: в hook event отсутствует обязательный tool_name.\n")
        return 2
    if not isinstance(data.get("tool_input"), dict):
        sys.stderr.write("protect-migrations: в hook event отсутствует обязательный tool_input.\n")
        return 2
    if data.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    if not isinstance(data["tool_input"].get("command"), str) or not data["tool_input"].get("command"):
        sys.stderr.write("protect-migrations: matched invocation не содержит обязательный command.\n")
        return 2

    cwd = Path(data.get("cwd") or ".").resolve()
    hit = offending(data["tool_input"]["command"], cwd)
    if not hit:
        return 0

    tool, sub = hit
    sys.stderr.write(
        "ЗАБЛОКИРОВАНО: `%s %s` меняет схему базы, а миграции применяет владелец.\n"
        "Проверить, одноразовая база или рабочая, из команды нельзя, а цена ошибки\n"
        "необратима — поэтому запрет механический, а не на усмотрение.\n"
        "\n"
        "Можно: создать миграцию, посмотреть историю и головы, свести модели со\n"
        "схемой (`revision`, `history`, `heads`, `current`, `show`, `makemigrations`).\n"
        "Дальше: положить файл миграции в ветку, назвать его в отчёте и в\n"
        "`flow/MANUAL.md` — применяет владелец.\n" % (tool, sub)
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
