"""Integración de output/ hacia el repo destino: detección de cambios pendientes y git (clone, rama, push).

El PR no se abre aquí sino en mcp_github.py. Todo es local hasta commit_and_push().
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME  = ".integration.json"
EXCLUDED_DIRS  = {"target", "node_modules", "dist", ".angular", "__pycache__", ".git", ".tmp"}
EXCLUDED_FILES = {".checkpoint.json", MANIFEST_NAME, "devops_report.md"}

_NAME_RE   = re.compile(r"^[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
_GIT_TIMEOUT = 180


@dataclass
class Draft:
    branch: str
    commit: str
    title: str
    body: str


@dataclass
class Staging:
    path: Path
    base: str
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)


# ── Cambios pendientes ─────────────────────────────────────────────

def output_files(output_dir: Path) -> dict[str, str]:
    """{ruta relativa posix: sha256} de lo integrable en output/."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(output_dir):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            if name in EXCLUDED_FILES or full.is_symlink():
                continue
            rel = full.relative_to(output_dir).as_posix()
            result[rel] = hashlib.sha256(full.read_bytes()).hexdigest()
    return result


def load_manifest(output_dir: Path) -> dict:
    try:
        return json.loads((output_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(output_dir: Path, files: dict[str, str], pr_url: str, branch: str) -> None:
    data = {
        "integrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pr_url": pr_url,
        "branch": branch,
        "files": files,
    }
    (output_dir / MANIFEST_NAME).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_changes(output_dir: Path) -> dict[str, list[str]]:
    """Archivos nuevos o modificados respecto a la última integración."""
    integrated = load_manifest(output_dir).get("files", {})
    current = output_files(output_dir)
    return {
        "new":      sorted(p for p in current if p not in integrated),
        "modified": sorted(p for p, h in current.items() if p in integrated and integrated[p] != h),
    }


# ── Borrador del agente ────────────────────────────────────────────

def slugify(text: str, default: str = "cambios") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40].strip("-")
    return slug or default


def parse_draft(text: str, fallback_title: str) -> Draft:
    """Lee BRANCH/COMMIT/TITLE y el cuerpo tras '---'; si faltan, usa valores seguros."""
    raw = str(text or "")
    fields = {m.group(1).upper(): m.group(2).strip().strip("`") for m in
              re.finditer(r"^\s*(BRANCH|COMMIT|TITLE):[ \t]*(.+)$", raw, re.MULTILINE)}
    parts = re.split(r"^\s*---\s*$", raw, maxsplit=1, flags=re.MULTILINE)
    body = parts[1].strip() if len(parts) == 2 else raw.strip()

    branch = fields.get("BRANCH", "").lower()
    if not valid_branch(branch):
        branch = f"crewai/{slugify(fallback_title)}"
    title = (fields.get("TITLE") or f"feat: {fallback_title}")[:100]
    return Draft(branch=branch, commit=(fields.get("COMMIT") or title)[:200], title=title, body=body)


def valid_branch(name: str) -> bool:
    return bool(name) and len(name) <= 80 and bool(_BRANCH_RE.match(name)) and not name.endswith(".lock")


def unique_branch(branch: str) -> str:
    return f"{branch}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def describe_changes(staging: Staging) -> str:
    lines = [f"A {p}" for p in staging.added] + [f"M {p}" for p in staging.modified]
    return (
        f"\n\n---\n## Cambios a integrar en la raíz del repo destino (rama base: {staging.base})\n"
        "A = archivo nuevo, M = reemplaza un archivo ya existente en el repo.\n" + "\n".join(lines)
    )


# ── Git ────────────────────────────────────────────────────────────

def repo_url(owner: str, repo: str) -> str:
    if not _NAME_RE.match(owner or "") or not _NAME_RE.match(repo or ""):
        raise ValueError(f"Owner/repo destino inválido: '{owner}/{repo}'. Revisa DEVOPS_TARGET_OWNER y DEVOPS_TARGET_REPO.")
    return f"https://github.com/{owner}/{repo}.git"


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} falló: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def stage_changes(output_dir: Path, files: dict[str, str], url: str, base: str) -> Staging:
    """Clona la rama base en un directorio temporal y copia los archivos a la raíz (sin commit)."""
    if not valid_branch(base):
        raise ValueError(f"Rama base inválida: '{base}'")
    workdir = Path(tempfile.mkdtemp(prefix="devops-"))
    staging = Staging(path=workdir / "repo", base=base)
    try:
        _git(["clone", "--depth", "1", "--branch", base, url, str(staging.path)])
        for rel in files:
            dest = staging.path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_dir / rel, dest)
        _git(["add", "-A"], staging.path)
        for line in _git(["diff", "--cached", "--name-status"], staging.path).splitlines():
            status, _, path = line.partition("\t")
            (staging.added if status.startswith("A") else staging.modified).append(path)
    except Exception:
        cleanup(staging)
        raise
    if not (staging.added or staging.modified):
        cleanup(staging)
        raise ValueError("El repo destino ya contiene exactamente estos archivos; no hay nada que integrar.")
    return staging


def commit_and_push(staging: Staging, branch: str, message: str) -> None:
    if not valid_branch(branch):
        raise ValueError(f"Nombre de rama inválido: '{branch}'")
    _git(["checkout", "-b", branch], staging.path)
    _git(["commit", "-m", message], staging.path)
    _git(["push", "-u", "origin", branch], staging.path)


def cleanup(staging: Staging) -> None:
    def _writable(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    shutil.rmtree(staging.path.parent, onerror=_writable)
