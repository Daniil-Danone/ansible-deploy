#!/usr/bin/env python3
"""
verify — механическая проверка дифа перед тем, как звать ревьюера.

Смысл: ревью стоит дорого, а половина того, за чем его зовут, проверяется
текстом и регулярками за ноль токенов. Сначала дешёвое, и только если оно
нашло подозрительное — дорогое.

  python .codex/tools/verify.py                 диф рабочего дерева против HEAD
  python .codex/tools/verify.py --base develop  диф ветки против develop
  python .codex/tools/verify.py --json          машинный вывод
  python .codex/tools/verify.py --fix           откатить файлы из одних пробелов

Код возврата: 0 — чисто, 1 — есть находки или диф задел зону риска
(тогда зовём reviewer), 2 — не смог проверить (не нашёл репозиторий).

Работает и в монорепозитории без git в корне: тогда обходит подкаталоги
первого уровня, в которых есть .git. Только stdlib.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Консоль Windows по умолчанию не в UTF-8: без этого вывод с кириллицей и
# типографикой падает с UnicodeEncodeError, а падение хука выглядит как
# «команда не отработала».
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Мусор, который не должен уезжать в ветку. Ищем только в ДОБАВЛЕННЫХ строках.
DEBRIS = [
    (r"\bconsole\.log\s*\(", "console.log"),
    (r"\bdebugger\b", "debugger"),
    (r"\bbreakpoint\s*\(", "breakpoint()"),
    (r"\bpdb\.set_trace\s*\(", "pdb.set_trace()"),
    (r"\bvar_dump\s*\(|\bdd\s*\(", "var_dump/dd"),
    (r"^\s*print\s*\(", "print("),
    (r"\.only\s*\(|\bfdescribe\b|\bfit\s*\(", "тест с .only/fdescribe"),
    (r"\bTODO\b|\bFIXME\b|\bXXX\b", "TODO/FIXME"),
]

SWALLOWED = [
    (r"except\s*:\s*$", "голый except:"),
    (r"except[^:]*:\s*\n\s*pass\b", "except … : pass"),
    (r"catch\s*\([^)]*\)\s*\{\s*\}", "пустой catch {}"),
    (r"\.catch\s*\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)", "пустой .catch(() => {})"),
]

SECRETS = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key|token|access[_-]?key)\s*[=:]\s*[\"'][^\"'\s]{8,}", "секрет в присвоении"),
    (r"\b[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b", "похоже на JWT"),
    (r"(?i)\bAKIA[0-9A-Z]{16}\b", "ключ AWS"),
]

DEPS = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "pyproject.toml", "poetry.lock", "uv.lock", "requirements.txt",
        "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile.lock")

BIG_FILE_LINES = 400  # диф на один файл больше этого — повод посмотреть глазами


def git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", str(repo)] + list(args),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def repos(root: Path):
    """Репозиторий в корне, иначе — все подкаталоги первого уровня с .git."""
    if (root / ".git").exists():
        return [root]
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / ".git").exists())


def risk_zones(root: Path):
    """Таблица «Зона | Пути» из flow/PROJECT.md → [(зона, [глоб, …]), …]."""
    p = root / "flow" / "PROJECT.md"
    if not p.is_file():
        return []
    out, inside = [], False
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^##\s+Зоны риска", line):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if not inside or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[1]) <= set("-: ") or cells[0].lower() == "зона":
            continue
        globs = [g.strip().strip("`") for g in re.split(r"[,;]| и ", cells[1]) if g.strip().strip("`")]
        if globs:
            out.append((cells[0], globs))
    return out


def matches(path: str, pattern: str) -> bool:
    """Глоб по пути.

    `app/billing/**` ловит и сам каталог, и всё внутри.
    `.../services/secrets/**` — сокращение «где-то в дереве»: в таблицах зон
    так пишут хвост длинного пути, и его надо понимать буквально так же.
    """
    import fnmatch
    pat = pattern.strip().rstrip("/")
    if not pat:
        return False
    if pat.startswith(".../"):
        tail = pat[4:]
        return any(matches(path[i:], tail) for i in range(len(path))
                   if i == 0 or path[i - 1] == "/")
    if pat.endswith("/**"):
        base = pat[:-3]
        return path == base or path.startswith(base + "/")
    if fnmatch.fnmatch(path, pat):
        return True
    stem = pat.rstrip("*").rstrip("/")
    return bool(stem) and path.startswith(stem + "/")


def changed_files(repo, base):
    rng = [base] if base else []
    names = git(repo, "diff", "--name-only", *rng).splitlines()
    if not base:
        names += git(repo, "diff", "--name-only", "--cached").splitlines()
    return sorted(set(n for n in names if n.strip()))


def numstat(repo, base):
    rng = [base] if base else []
    out = {}
    raw = git(repo, "diff", "--numstat", *rng)
    if not base:
        raw += git(repo, "diff", "--numstat", "--cached")
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            add, rem, name = int(parts[0]), int(parts[1]), parts[2]
            a, r = out.get(name, (0, 0))
            out[name] = (a + add, r + rem)
    return out


def file_diff(repo, base, path):
    rng = [base] if base else []
    d = git(repo, "diff", "-U0", *rng, "--", path)
    if not base:
        d += git(repo, "diff", "-U0", "--cached", "--", path)
    return d


def added_lines(diff):
    return [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]


def whitespace_only(repo, base, path):
    """Файл, в котором изменились только пробелы и переносы строк."""
    rng = [base] if base else []
    real = git(repo, "diff", "--numstat", "--ignore-all-space",
               "--ignore-blank-lines", *rng, "--", path).strip()
    if not base and not real:
        real = git(repo, "diff", "--numstat", "--cached", "--ignore-all-space",
                   "--ignore-blank-lines", "--", path).strip()
    return real == ""


def revert(repo, path):
    """Вернуть файл к состоянию HEAD — и в индексе, и в рабочем дереве."""
    out = subprocess.run(["git", "-C", str(repo), "restore", "--staged", "--worktree", "--", path],
                         capture_output=True, text=True)
    if out.returncode != 0:  # git старше 2.23
        subprocess.run(["git", "-C", str(repo), "reset", "-q", "HEAD", "--", path],
                       capture_output=True, text=True)
        out = subprocess.run(["git", "-C", str(repo), "checkout", "--", path],
                             capture_output=True, text=True)
    return out.returncode == 0


def check_repo(root: Path, repo: Path, base, zones, fix=False):
    findings, touched = [], {}
    rel_repo = "" if repo == root else repo.name + "/"
    files = changed_files(repo, base)
    if not files:
        return findings, touched, files
    stats = numstat(repo, base)

    for f in files:
        full = rel_repo + f
        add, rem = stats.get(f, (0, 0))

        for zone, globs in zones:
            # Путь в таблице зон могут записать и от корня монорепозитория, и от
            # корня подрепозитория — принимаем оба написания.
            if any(matches(full, g) or matches(f, g) for g in globs):
                touched.setdefault(zone, []).append(full)

        if f.startswith(".env") or "/.env" in f:
            findings.append(("blocker", full, "в дифе файл окружения"))

        if os.path.basename(f) in DEPS:
            findings.append(("attention", full, "изменены зависимости — проверьте, что новая нужна"))

        if re.search(r"(^|/)(migrations?|alembic|versions)/", f):
            findings.append(("attention", full, "миграция в дифе — прогоняет владелец, не агент"))

        if add + rem == 0:
            continue

        if whitespace_only(repo, base, f):
            if fix and not base and revert(repo, f):
                findings.append(("fixed", full,
                                 "изменились только пробелы и переносы строк (+%d/-%d) — откачено" % (add, rem)))
            else:
                findings.append(("blocker", full,
                                 "изменились только пробелы и переносы строк (+%d/-%d) — откатите файл" % (add, rem)))
            continue

        if add + rem > BIG_FILE_LINES:
            findings.append(("attention", full, "крупный диф: +%d/-%d строк" % (add, rem)))

        diff = file_diff(repo, base, f)
        plus = added_lines(diff)
        body = "\n".join(plus)

        for rx, name in DEBRIS:
            hits = sum(1 for l in plus if re.search(rx, l))
            if hits:
                findings.append(("attention", full, "добавлено: %s x%d" % (name, hits)))
        for rx, name in SWALLOWED:
            if re.search(rx, body, re.M):
                findings.append(("attention", full, "проглоченная ошибка: %s" % name))
        for rx, name in SECRETS:
            if re.search(rx, body):
                findings.append(("blocker", full, "возможный секрет в дифе: %s" % name))

    return findings, touched, files


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--base", default=None, help="с чем сравнивать (например develop); по умолчанию — рабочее дерево против HEAD")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix", action="store_true",
                    help="откатить файлы, где изменились только пробелы (только против HEAD)")
    a = ap.parse_args()

    root = Path(os.environ.get("CODEX_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    rs = repos(root)
    if not rs:
        print("verify: git-репозиторий не найден ни в корне, ни в подкаталогах первого уровня")
        return 2

    zones = risk_zones(root)
    all_f, all_z, all_files = [], {}, []
    for repo in rs:
        f, z, files = check_repo(root, repo, a.base, zones, fix=a.fix)
        all_f += f
        for k, v in z.items():
            all_z.setdefault(k, []).extend(v)
        all_files += [("" if repo == root else repo.name + "/") + x for x in files]

    blockers = [x for x in all_f if x[0] == "blocker"]
    attention = [x for x in all_f if x[0] == "attention"]
    fixed = [x for x in all_f if x[0] == "fixed"]

    if a.json:
        print(json.dumps({"files": all_files, "blockers": blockers, "fixed": fixed,
                          "attention": attention, "zones": all_z}, ensure_ascii=False, indent=1))
    else:
        print("verify: файлов в дифе — %d" % len(all_files))
        for rows, title in ((blockers, "БЛОКИРУЕТ"), (fixed, "починено"), (attention, "внимание")):
            for _, path, msg in rows:
                print("  %-10s %s — %s" % (title, path, msg))
        if all_z:
            print("  зоны риска: " + ", ".join(
                "%s (%d файл.)" % (k, len(set(v))) for k, v in sorted(all_z.items())))
        if not all_f and not all_z:
            print("  чисто — ревьюер не нужен")

    # Починенное само по себе повода звать ревьюера не даёт.
    return 1 if (blockers or attention or all_z) else 0


if __name__ == "__main__":
    sys.exit(main())
