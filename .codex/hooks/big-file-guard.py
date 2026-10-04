#!/usr/bin/env python3
"""
big-file-guard — не давать делать дорогое дешёвым способом.

Две проверки, обе про цену вызова: первая про контекст, вторая про время.

Один собранный макет на 6 МБ, прочитанный целиком, стоит дороже, чем весь
остальной шаг. Такие файлы почти всегда производные: сборка, экспорт
артефакта, бандл, дамп. Нужный кусок из них достаётся `grep`/`sed`, а если
нужен весь файл — он слишком большой и его надо разрезать.

Вторая: цикл ожидания в шелле (`until … sleep`, `while … sleep`). Агент
запускает что-то фоном и садится ждать в шелле, держа вызов инструмента
открытым. По замерам такие циклы съедали по 5 минут каждый, не делая ничего:
ожидание в шелле — это не работа, это просто потерянные минуты.

Режим:
  python big-file-guard.py --pre-tool   хук PreToolUse(Read|Bash): заблокировать
                                        (exit 2) и подсказать дешёвый способ

Порог — 200 КБ, меняется переменной окружения FLOW_MAX_READ_BYTES.
Хук читает JSON события на stdin. Корень берётся из `cwd` события, затем из
CODEX_PROJECT_DIR/CLAUDE_PROJECT_DIR, затем из текущего каталога. Только stdlib.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

DEFAULT_LIMIT = 200 * 1024

# Команды, которые печатают файл целиком. Всё остальное (grep, sed -n, head -n,
# wc, du, awk с условием) само себя ограничивает и здесь не разбирается.
WHOLE_FILE_CMDS = {"cat", "bat", "type", "Get-Content", "gc"}
# Цикл ожидания: until/while с проверкой и sleep внутри.
BUSY_WAIT = re.compile(r"\b(until|while)\b[^\n]{0,200}?;\s*do\b[^\n]{0,200}?\bsleep\b", re.S)

# Флаги, превращающие «целиком» в «кусок».
LIMIT_FLAGS = re.compile(r"(^|\s)-(n|c)\b|--lines\b|--bytes\b|-TotalCount\b|-Tail\b|-First\b")


def limit() -> int:
    raw = os.environ.get("FLOW_MAX_READ_BYTES", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_LIMIT


def project_root(event=None) -> Path:
    event = event if isinstance(event, dict) else {}
    start = Path(
        event.get("cwd")
        or os.environ.get("CODEX_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    ).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "flow" / "PROJECT.md").is_file() and (
            (candidate / ".codex").is_dir() or (candidate / ".claude").is_dir()
        ):
            return candidate
    return start


def size_of(root: Path, raw: str):
    """Размер файла в байтах либо None, если это не существующий файл."""
    if not raw or raw.startswith("-"):
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = root / raw
    try:
        if p.is_file():
            return p.stat().st_size
    except OSError:
        pass
    return None


def too_big_in_bash(root: Path, command: str, cap: int):
    """Пути в команде, которые печатаются целиком и тяжелее порога."""
    hits = []
    for part in re.split(r"[|;&]+|\$\(|\)", command or ""):
        part = part.strip()
        if not part or LIMIT_FLAGS.search(part):
            continue
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        if not words or words[0] not in WHOLE_FILE_CMDS:
            continue
        for w in words[1:]:
            n = size_of(root, w)
            if n is not None and n > cap:
                hits.append((w, n))
    return hits


def block(lines):
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


def main() -> int:
    if "--pre-tool" not in sys.argv:
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    root = project_root(data)
    cap = limit()
    kb = cap // 1024
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}

    if tool == "Read":
        # Частичное чтение разрешено: у Read есть offset/limit.
        if inp.get("offset") or inp.get("limit"):
            return 0
        n = size_of(root, inp.get("file_path") or "")
        if n is not None and n > cap:
            block([
                f"ЗАБЛОКИРОВАНО: файл {n // 1024} КБ, порог {kb} КБ.",
                f"  {inp.get('file_path')}",
                "Скорее всего это производное: сборка, экспорт артефакта, бандл или дамп.",
                "Дешёвые способы: grep с контекстом, sed -n 'A,Bp', Read с offset/limit,",
                "или разрезать файл, если он читается целиком постоянно.",
            ])
        return 0

    if tool in ("Bash", "PowerShell"):
        cmd = inp.get("command") or ""
        if BUSY_WAIT.search(cmd):
            block([
                "ЗАБЛОКИРОВАНО: цикл ожидания в шелле.",
                "Ожидание в шелле не работа: вызов висит, минуты идут, не делается ничего.",
                "Как правильно:",
                "  — команду, результат которой нужен сейчас, запускать обычным вызовом;",
                "  — долгую команду запускать в фоне и идти делать следующее,",
                "    а результат забрать, когда о нём сообщат;",
                "  — не опрашивать файл в цикле: если нечем заняться, значит",
                "    эту команду надо было запустить обычным вызовом.",
            ])
        hits = too_big_in_bash(root, cmd, cap)
        if hits:
            lines = [f"ЗАБЛОКИРОВАНО: команда печатает файл целиком, порог {kb} КБ."]
            for path, n in hits:
                lines.append(f"  {path} — {n // 1024} КБ")
            lines.append("Возьмите нужный кусок: grep с контекстом, sed -n 'A,Bp', head -c.")
            block(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
