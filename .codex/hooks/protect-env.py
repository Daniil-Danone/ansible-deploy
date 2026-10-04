#!/usr/bin/env python3
"""Блокирует чтение и изменение локальных файлов с секретами `.env`."""

import json
import os
import re
import shlex
import sys

try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PROTECTED = re.compile(r"(?:^|[/\\])\.env(?:\.(?:prod|production|local|bak-[^/\\\s]+))?$", re.I)
SHELL_ENV = re.compile(r"(?<![\w.-])(?:[^\s\"';|&<>]*[/\\])?\.env(?:\.(?:prod|production|local|bak-[\w.-]+))?(?![\w.-])", re.I)
UNSAFE_SEARCH = re.compile(r"\b(?:grep\s+[^\n;|]*-[^\s]*[rR]|rg\s+[^\n;|]*(?:-u|--hidden|--no-ignore)|findstr\s+[^\n;|]*/s)\b", re.I)
SAFE_EXCLUDE = re.compile(r"--exclude(?:=|\s+)[\"']?\.env\*", re.I)
WRITE = re.compile(
    r"\b(?:rm|cp|mv|tee|touch|truncate|del|erase|move|copy|Set-Content|Add-Content|"
    r"Clear-Content|Out-File|Remove-Item|Move-Item|Copy-Item|Rename-Item|New-Item)\b|>>?", re.I
)
READ = re.compile(
    r"\b(?:cat|type|Get-Content|gc|head|tail|less|more|grep|rg|findstr|Select-String|sls|"
    r"sed|awk|python|py|node|source)\b|(?:^|[;&|])\s*\.\s+", re.I
)
PS_RECURSIVE_SELECT = re.compile(
    r"\b(?:Get-ChildItem|gci|dir)\b[^|;\n]*-(?:Recurse|rec\w*|r)\b[^|;\n]*"
    r"\|\s*(?:Select-String|sls)\b",
    re.I,
)
DOCKER_CONFIG = re.compile(r"\bdocker(?:-compose|\s+compose)\b[^;|\n]*\bconfig\b", re.I)
DOCKER_SAFE = re.compile(r"\s(?:-q|--quiet|--services|--volumes|--images|--profiles)(?:\s|$)", re.I)
MATCHED_TOOLS = {
    "Read", "NotebookRead", "Write", "Edit", "NotebookEdit", "Grep", "Bash", "PowerShell", "apply_patch"
}


def destructive_git(command):
    """Описание опасной git-команды или None; ничего не выполняет."""
    for segment in re.split(r"[|;&\n]+", command or ""):
        try:
            words = shlex.split(segment, posix=False)
        except ValueError:
            words = segment.split()
        words = [word.strip("\"'") for word in words]
        for start, word in enumerate(words):
            executable = word.replace("\\", "/").rsplit("/", 1)[-1]
            executable = re.sub(r"\.(?:exe|cmd|bat)$", "", executable, flags=re.I).casefold()
            if executable != "git":
                continue
            i = start + 1
            while i < len(words) and words[i].startswith("-"):
                option = words[i]
                i += 1
                if option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i < len(words):
                    i += 1
            if i >= len(words):
                continue
            subcommand = words[i].lower()
            args = words[i + 1:]
            if subcommand == "clean" and any(
                arg in ("-x", "-X") or bool(re.fullmatch(r"-[^-]*[xX][A-Za-z]*", arg))
                for arg in args
            ):
                return "git clean с -x/-X может удалить ignored-файлы, включая `.env`"
            if subcommand == "stash" and any(
                arg == "--all" or bool(re.fullmatch(r"-[^-]*a[A-Za-z]*", arg))
                for arg in args
            ):
                return "git stash --all/-a может спрятать ignored-файлы, включая `.env`"
    return None


def protected(path):
    return isinstance(path, str) and bool(PROTECTED.search(path.replace("\\", "/")))


def reason(tool, data):
    data = data if isinstance(data, dict) else {}
    if tool in ("Read", "NotebookRead", "Write", "Edit", "NotebookEdit"):
        path = data.get("file_path") or data.get("notebook_path") or ""
        if protected(path):
            return "protect-env: `.env` содержит секреты владельца; чтение и изменение заблокированы. Используй `.env.example`."
    if tool == "Grep" and (protected(data.get("path", "")) or ".env" in str(data.get("glob", ""))):
        return "protect-env: поиск может вывести секреты из `.env`; используй git grep или исключи `.env*`."
    if tool == "apply_patch":
        patch = data.get("command", "")
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", patch, re.M)
        paths += re.findall(r"^\*\*\* Move to:\s*(.+?)\s*$", patch, re.M)
        if any(protected(path) for path in paths):
            return "protect-env: apply_patch нацелен на защищённый `.env`; изменение заблокировано."
    if tool in ("Bash", "PowerShell"):
        cmd = str(data.get("command", ""))
        git_reason = destructive_git(cmd)
        if git_reason:
            return "protect-env: " + git_reason + "; команда заблокирована."
        if UNSAFE_SEARCH.search(cmd) and not SAFE_EXCLUDE.search(cmd):
            return "protect-env: рекурсивный поиск без исключения `.env*` заблокирован."
        if PS_RECURSIVE_SELECT.search(cmd):
            return "protect-env: рекурсивный PowerShell-поиск может вывести значения из `.env`; вызов заблокирован."
        if SHELL_ENV.search(cmd) and (READ.search(cmd) or WRITE.search(cmd)):
            return "protect-env: команда читает или изменяет защищённый `.env`; используй `.env.example`."
        m = DOCKER_CONFIG.search(cmd)
        if m and not DOCKER_SAFE.search(m.group(0)):
            return "protect-env: `docker compose config` может вывести секреты; используй `config -q`."
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, json.JSONDecodeError):
        sys.stderr.write("protect-env: некорректный или пустой hook event; безопаснее заблокировать вызов.\n")
        return 2
    if not isinstance(event, dict) or not isinstance(event.get("tool_name"), str) or not event.get("tool_name"):
        sys.stderr.write("protect-env: в hook event отсутствует обязательный tool_name.\n")
        return 2
    if not isinstance(event.get("tool_input"), dict):
        sys.stderr.write("protect-env: в hook event отсутствует обязательный tool_input.\n")
        return 2
    tool = event["tool_name"]
    tool_input = event["tool_input"]
    if tool in MATCHED_TOOLS:
        required = "command" if tool in ("Bash", "PowerShell", "apply_patch") else (
            "pattern" if tool == "Grep" else ("notebook_path" if tool in ("NotebookRead", "NotebookEdit") else "file_path")
        )
        if required not in tool_input or not isinstance(tool_input.get(required), str) or not tool_input.get(required):
            sys.stderr.write(f"protect-env: matched invocation не содержит обязательный {required}.\n")
            return 2
    why = reason(tool, tool_input)
    if why:
        sys.stderr.write(why + os.linesep)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
