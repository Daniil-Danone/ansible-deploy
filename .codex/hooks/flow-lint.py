#!/usr/bin/env python3
"""
flow-lint — механическая проверка планов flow/ и хуки агента.

Режимы:
  python flow-lint.py                 проверить все активные планы, вывести отчёт
  python flow-lint.py --pre-commit    хук PreToolUse(Bash): если команда — git commit,
                                      проверить планы; при ошибке заблокировать (exit 2)
  python flow-lint.py --session-start хук SessionStart: напечатать текущий шаг
  python flow-lint.py --pre-compact   хук PreCompact: напомнить про /save

Хуки читают JSON события на stdin. Корень берётся из `cwd` события, затем из
CODEX_PROJECT_DIR/CLAUDE_PROJECT_DIR, затем из текущего каталога. Только stdlib.
"""

import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

STEP_RE = re.compile(r"^- \[(?P<mark>[ x~S!])\]\s*(?P<flags>(?:[∥⏸]\s*)*)(?P<num>\d+)\.\s*(?P<title>.+?)\s*$")
CRIT_RE = re.compile(r"^\s{2,}Готово:\s*(?P<crit>.+?)\s*$")
RUNS_HEADING_RE = re.compile(r"^##\s+Заходы\s*$")
RUN_RE = re.compile(r"^- (?P<a>\d+)(?:\s*[–-]\s*(?P<b>\d+))?(?!\d)")
VAGUE = (
    "работает корректно", "работает правильно", "всё работает", "все работает",
    "покрыто тестами", "покрыт тестами", "всё ок", "все ок", "готово", "сделано",
    "реализовано", "протестировано",
    # Утверждения про изменение, а не про значение: остаются зелёными и тогда,
    # когда нужного поведения нет вовсе. См. gates.md, «Тест утверждает
    # значение, а не изменение».
    "изменился", "изменилось", "изменилась", "поменялся", "поменялось",
    "отличается", "стало другим", "не пустой", "не пуст", "не пустая",
    "не падает", "отрабатывает", "появился", "появилось", "отрисовался",
)


_hook_input = None


def read_hook_input():
    global _hook_input
    if _hook_input is not None:
        return _hook_input
    if sys.stdin.isatty():
        _hook_input = {}
        return _hook_input
    try:
        raw = sys.stdin.read()
        _hook_input = json.loads(raw) if raw.strip() else {}
    except Exception:
        _hook_input = {}
    return _hook_input


def project_root() -> Path:
    event = read_hook_input()
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


def runtime_dir(root: Path) -> str:
    return ".codex" if (root / ".codex").is_dir() else ".claude"


def active_plans(root: Path):
    flow = root / "flow"
    if not flow.is_dir():
        return []
    plans = []
    for d in sorted(flow.iterdir()):
        if d.is_dir() and d.name != "done" and (d / "plan.md").is_file():
            plans.append(d / "plan.md")
    return plans


def parse_runs(plan: Path):
    """Вернуть заходы из раздела «## Заходы»: dict(first, last, line). Нет раздела — пустой список."""
    runs = []
    inside = False
    for i, line in enumerate(plan.read_text(encoding="utf-8").splitlines()):
        if RUNS_HEADING_RE.match(line):
            inside = True
            continue
        if inside and line.startswith("#"):
            break
        if not inside:
            continue
        m = RUN_RE.match(line)
        if m:
            first = int(m.group("a"))
            runs.append({"first": first, "last": int(m.group("b") or first), "line": i + 1})
    return runs


def lint_runs(rel, steps, runs):
    errors = []
    if not runs:
        return errors
    by_num = {s["num"]: s for s in steps}
    prev_last = 0
    covered = set()
    for r in runs:
        span = f"{r['first']}–{r['last']}" if r["first"] != r["last"] else str(r["first"])
        if r["first"] > r["last"]:
            errors.append(f"{rel}:{r['line']}: заход {span} — начало больше конца")
            continue
        if r["first"] <= prev_last:
            errors.append(f"{rel}:{r['line']}: заход {span} пересекается с предыдущим или идёт не по порядку")
        missing = [n for n in range(r["first"], r["last"] + 1) if n not in by_num]
        if missing:
            errors.append(f"{rel}:{r['line']}: заход {span} ссылается на несуществующие шаги: {', '.join(map(str, missing))}")
        for n in range(r["first"], r["last"]):
            if n in by_num and by_num[n]["pause"]:
                errors.append(f"{rel}:{r['line']}: шаг ⏸{n} не последний в заходе {span} — после стоп-точки владельца заход кончается")
        covered.update(range(r["first"], r["last"] + 1))
        prev_last = max(prev_last, r["last"])
    uncovered = [s["num"] for s in steps if s["num"] not in covered]
    if uncovered:
        errors.append(f"{rel}: раздел «Заходы» не покрывает шаги: {', '.join(map(str, uncovered))}")
    return errors


def run_of(runs, num):
    return next((r for r in runs if r["first"] <= num <= r["last"]), None)


def parse(plan: Path):
    """Вернуть список шагов: dict(mark, num, title, crit, line, parallel, pause)."""
    steps = []
    lines = plan.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        m = STEP_RE.match(line)
        if not m:
            continue
        crit = None
        for j in range(i + 1, min(i + 3, len(lines))):
            c = CRIT_RE.match(lines[j])
            if c:
                crit = c.group("crit")
                break
            if STEP_RE.match(lines[j]):
                break
        steps.append({
            "mark": m.group("mark"),
            "num": int(m.group("num")),
            "title": m.group("title"),
            "crit": crit,
            "line": i + 1,
            "parallel": "∥" in m.group("flags"),
            "pause": "⏸" in m.group("flags"),
        })
    return steps


def has_proof(crit: str) -> bool:
    """Есть ли в критерии то, что можно выполнить и увидеть.

    Ссылка на тест `T12` годится. Бэктик сам по себе — нет: критерий
    «Готово: `готово`» проходил проверку, ничего при этом не доказывая.
    Годится содержимое бэктиков, похожее на команду, путь или наблюдение:
    достаточно длинное, с пробелом, слэшем, точкой или скобкой внутри, и не
    из списка отговорок.
    """
    if re.search(r"\bT\d+\b", crit):
        return True
    for seg in re.findall(r"`([^`]+)`", crit):
        seg = seg.strip()
        if len(seg) < 6 or seg.lower() in VAGUE:
            continue
        if re.search(r"[ /.():\\-]", seg):
            return True
    return False


def lint(plan: Path):
    errors = []
    steps = parse(plan)
    rel = plan.relative_to(project_root()) if plan.is_relative_to(project_root()) else plan
    if not steps:
        return [f"{rel}: не найдено ни одного шага вида `- [ ] N. …`"]

    current = [s for s in steps if s["mark"] == "~"]
    pending = [s for s in steps if s["mark"] == " "]

    if len(current) > 1:
        nums = ", ".join(str(s["num"]) for s in current)
        errors.append(f"{rel}: больше одного текущего шага [~]: {nums}")
    if pending and not current:
        errors.append(f"{rel}: есть шаги [ ], но ни один не помечен текущим [~]")

    if current:
        first_cur = min(s["line"] for s in current)
        skipped = [s for s in pending if s["line"] < first_cur and not s["parallel"]]
        for s in skipped:
            errors.append(f"{rel}:{s['line']}: шаг {s['num']} [ ] стоит выше текущего [~] — шаг проскочили")

    log_dir = plan.parent / "log"
    for s in steps:
        if s["crit"] is None:
            errors.append(f"{rel}:{s['line']}: у шага {s['num']} нет строки «Готово:»")
        else:
            low = s["crit"].lower()
            has_ref = has_proof(s["crit"])
            vague = any(v in low for v in VAGUE)
            if not has_ref and (len(low) < 10 or vague):
                errors.append(
                    f"{rel}:{s['line']}: критерий шага {s['num']} невыполним — "
                    f"нужна команда или наблюдение, а не «{s['crit'][:40]}»"
                )
        if s["mark"] in "S!" and " — " not in s["title"]:
            errors.append(f"{rel}:{s['line']}: шаг {s['num']} помечен [{s['mark']}] без причины после « — »")
        if s["mark"] == "x":
            pattern = f"{s['num']:02d}-*.md"
            if not log_dir.is_dir() or not list(log_dir.glob(pattern)):
                errors.append(f"{rel}:{s['line']}: шаг {s['num']} помечен [x], но нет log/{pattern}")
    errors.extend(lint_runs(rel, steps, parse_runs(plan)))
    return errors


def summary(plan: Path) -> str:
    steps = parse(plan)
    done = sum(1 for s in steps if s["mark"] in "xS")
    cur = next((s for s in steps if s["mark"] == "~"), None)
    nxt = next((s for s in steps if s["mark"] == " "), None)
    name = plan.parent.name
    if cur:
        run = run_of(parse_runs(plan), cur["num"])
        hint = f", заход /go {run['first']}-{run['last']}" if run and run["first"] != run["last"] else ""
        return f"{name}: шаг {cur['num']}/{len(steps)} [~] {cur['title']}  ({done} закрыто{hint})"
    if nxt:
        return f"{name}: следующий {nxt['num']}/{len(steps)} {nxt['title']}  ({done} закрыто, текущий не помечен)"
    return f"{name}: все {len(steps)} шагов закрыты — пора /ship"


def in_progress(plan: Path) -> bool:
    """В плане есть шаг в работе."""
    return any(st["mark"] == "~" for st in parse(plan))


def zone_gate(root: Path):
    """Зоны риска, задетые тем, что готовится к коммиту, и не отмеченные в логе.

    Диф считает runtime-инструмент `tools/verify.py` — он же разбирает таблицу «Зоны
    риска» из flow/PROJECT.md и умеет монорепозиторий. Нет его — проверки нет.
    Отметка о гейте — строка «Гейты:» в самом свежем файле log/ текущего плана.
    """
    import importlib.util

    tool = root / runtime_dir(root) / "tools" / "verify.py"
    if not tool.is_file():
        return []
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # не сорить __pycache__ рядом с инструментом
    try:
        spec = importlib.util.spec_from_file_location("flow_verify", tool)
        v = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(v)
        zones = v.risk_zones(root)
        if not zones:
            return []
        touched = set()
        for repo in v.repos(root):
            prefix = "" if repo == root else repo.name + "/"
            staged = v.git(repo, "diff", "--name-only", "--cached").splitlines()
            for f in (x for x in staged if x.strip()):
                for zone, globs in zones:
                    if any(v.matches(prefix + f, g) or v.matches(f, g) for g in globs):
                        touched.add(zone)
    except Exception:
        return []
    finally:
        sys.dont_write_bytecode = saved
    if not touched:
        return []

    logged = ""
    for plan in active_plans(root):
        if not in_progress(plan):
            continue
        log_dir = plan.parent / "log"
        files = sorted(log_dir.glob("*.md"), key=lambda f: f.stat().st_mtime) if log_dir.is_dir() else []
        if files:
            for line in files[-1].read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().lower().startswith("гейты:"):
                    logged += " " + line.lower()
    return sorted(z for z in touched if z.lower() not in logged)


def context_size(transcript_path) -> int:
    """Сколько токенов обрабатывается на последнем ходу этой сессии.

    Ход стоит тем дольше, чем больше контекст, а контекст в длинной сессии
    растёт монотонно: к пятым суткам он втрое больше, чем в первый час, и
    каждый ход втрое дольше. Число берём из последней записи транскрипта —
    его пишет runtime агента, если путь к транскрипту доступен.
    """
    if not transcript_path:
        return 0
    p = Path(transcript_path)
    if not p.is_file():
        return 0
    try:
        tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return 0
    for line in reversed(tail):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        u = (d.get("message") or {}).get("usage") or {}
        cc = u.get("cache_creation")
        created = sum(cc.values()) if isinstance(cc, dict) else 0
        n = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + created
        if n:
            return n
    return 0


def context_note(data) -> str:
    """Строка-напоминание, если контекст уже дорогой. Пустая, если всё в норме."""
    try:
        warn = int(os.environ.get("FLOW_CONTEXT_WARN", "150000"))
    except ValueError:
        warn = 150000
    n = context_size(data.get("transcript_path"))
    if n < warn:
        return ""
    return ("flow: контекст сессии ~%d тыс. токенов — столько обрабатывается на каждом ходу.\n"
            "  Шаг закрыт: самое время /save и /clear. Следующий заход в свежей сессии\n"
            "  пойдёт заметно быстрее — контекст начнётся с нуля, а не с этой отметки.\n" % (n // 1000))


def closed_iterations(root: Path):
    """Слоги итераций, уже перенесённых в flow/done/."""
    done = root / "flow" / "done"
    if not done.is_dir():
        return set()
    return {d.name for d in done.iterdir() if d.is_dir()}


def resurrected(root: Path, paths):
    """Пути, ведущие в итерацию, которая уже закрыта и лежит в done/.

    Логи пишут ссылки на кадры относительным путём (`log/12-ship/x.png`).
    Пока итерация активна, путь верен; после переезда в `done/` он начинает
    указывать мимо, и агент, читающий архивный лог, воссоздаёт каталог по
    старому адресу. Каталог-призрак потом путает и владельца, и /check.
    """
    closed = closed_iterations(root)
    if not closed:
        return []
    hits = []
    for p in paths:
        norm = str(p).replace("\\", "/")
        m = re.search(r"(?:^|/)flow/([^/]+)/", norm)
        if m and m.group(1) in closed and "/flow/done/" not in "/" + norm:
            hits.append((m.group(1), norm))
    return hits


def is_git_commit(cmd: str) -> bool:
    """Команда действительно коммитит.

    Поиск подстроки «git … commit» ловил и `git log --grep commit`, и слово
    commit внутри сообщения. Поэтому разбираем: находим `git`, пропускаем его
    собственные опции (включая парные -C и -c) и смотрим, что первое слово
    после них — именно `commit`.
    """
    for seg in re.split(r"[|;&\n]+", cmd):
        words = seg.strip().split()
        if not words:
            continue
        idx = [k for k, w in enumerate(words) if w == "git" or w.endswith("/git")]
        if not idx:
            continue
        i = idx[0] + 1
        while i < len(words) and words[i].startswith("-"):
            if words[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                i += 2
            else:
                i += 1
        if i < len(words) and words[i] == "commit":
            return True
    return False


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) > 1 and sys.argv[1] == "--context":
        data = read_hook_input() if not sys.stdin.isatty() else {}
        n = context_size(data.get("transcript_path") or (sys.argv[2] if len(sys.argv) > 2 else None))
        print("контекст последнего хода: %s токенов" % ("{:,}".format(n).replace(",", " ") if n else "не определён"))
        return 0

    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    root = project_root()
    plans = active_plans(root)

    if mode == "--session-start":
        ghosts = [d.name for d in (root / "flow").iterdir()
                  if d.is_dir() and d.name != "done" and d.name in closed_iterations(root)] \
            if (root / "flow").is_dir() else []
        if ghosts:
            print("flow: каталоги закрытых итераций воскресли в flow/ — %s. "
                  "Настоящие лежат в flow/done/, эти можно удалить." % ", ".join(ghosts))
        if plans:
            lines = ["flow: активные итерации —"] + ["  " + summary(p) for p in plans]
            errs = [e for p in plans for e in lint(p)]
            if errs:
                lines.append(f"  ! flow-lint: {len(errs)} проблем(ы) в планах, запусти python {runtime_dir(root)}/hooks/flow-lint.py")
            print("\n".join(lines))
        return 0

    if mode == "--pre-compact":
        if plans:
            print("flow: перед сжатием контекста запусти /save — решения и грабли этой сессии иначе потеряются.")
        return 0

    if mode == "--pre-write":
        data = read_hook_input()
        tool = data.get("tool_name") or ""
        inp = data.get("tool_input") or {}
        if tool in ("Write", "Edit", "NotebookEdit"):
            cand = [inp.get("file_path") or ""]
        elif tool in ("Bash", "PowerShell"):
            cand = re.findall(r"[^\s\"'<>|]*flow[/\\][^\s\"'<>|]*", str(inp.get("command", "")))
        else:
            return 0
        hits = resurrected(root, [c for c in cand if c])
        if hits:
            sys.stderr.write("flow-lint: итерация %s закрыта и лежит в flow/done/ — писать в неё нельзя.\n"
                             % hits[0][0])
            for slug, p in hits[:5]:
                sys.stderr.write("  %s\n" % p)
            sys.stderr.write(
                "Скорее всего путь взят из архивного лога, где кадры записаны\n"
                "относительно итерации (`log/NN-шаг/имя.png`). После переезда в done/\n"
                "такой путь указывает мимо. Нужный файл — в flow/done/%s/…;\n"
                "новое пишется в каталог текущей итерации.\n" % hits[0][0])
            return 2
        return 0

    if mode == "--pre-commit":
        data = read_hook_input()
        if data.get("tool_name") != "Bash":
            return 0
        cmd = str(data.get("tool_input", {}).get("command", ""))
        if not is_git_commit(cmd):
            return 0
        # Блокирует только план, по которому идёт работа. Заброшенный план из
        # прошлой итерации не должен запирать коммиты в новой — про него
        # достаточно напомнить.
        working = [p for p in plans if in_progress(p)]
        strict = working or plans
        errs = [e for p in strict for e in lint(p)]
        if errs:
            sys.stderr.write("flow-lint: коммит заблокирован, план не в порядке:\n")
            for e in errs:
                sys.stderr.write(f"  - {e}\n")
            sys.stderr.write("Исправь plan.md (или log/) и повтори коммит.\n")
            return 2

        ungated = zone_gate(root)
        if ungated:
            sys.stderr.write("flow-lint: коммит заблокирован, диф задел зоны риска без отметки о гейте:\n")
            for z in ungated:
                sys.stderr.write(f"  - {z}\n")
            sys.stderr.write(
                f"Прогони гейты уровня 1 по {runtime_dir(root)}/guidelines/gates.md и запиши строку\n"
                "в log/ текущего шага, например:\n"
                "  Гейты: зона «деньги» — reviewer чисто, premortem чисто\n"
            )
            return 2

        note = context_note(data)
        if note:
            print(note.rstrip())

        stale = [p for p in plans if p not in strict]
        for p in stale:
            if lint(p):
                rel = p.relative_to(root) if p.is_relative_to(root) else p
                print(f"flow-lint: план {rel} не в порядке, но работа идёт не по нему — коммит пропущен")
        return 0

    # --check
    if not plans:
        print("flow-lint: активных планов нет (flow/*/plan.md)")
        return 0
    total = 0
    for p in plans:
        errs = lint(p)
        total += len(errs)
        print(summary(p))
        for e in errs:
            print(f"  - {e}")
    print("flow-lint: OK" if total == 0 else f"flow-lint: {total} проблем(ы)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
