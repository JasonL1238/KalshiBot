"""Repo rules that are cheap to check: agent-doc tree, attribution, ignores, layering."""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
HOOK = HOOKS / "commit-msg"


def _patterns(name: str) -> list[str]:
    return [p for p in (HOOKS / name).read_text().split("\n") if p]


AI_IDENT = re.compile("|".join(_patterns("ai-identities.txt")), re.I)
AI_LINE = re.compile("|".join(f"(?:{p})" for p in _patterns("ai-attribution.txt")
                              + [rf"^[a-z-]+-by:.*<[^>]*(?:{AI_IDENT.pattern})>"]), re.I | re.M)
LINK = re.compile(r"\]\(([^)#\s]+)")
MAP_ROW = re.compile(r"^\|\s*\[?`?([^`|\]\s]+)`?\]?", re.M)  # first cell of a table row
LAYERS = {  # package -> packages it must never import (directly or transitively)
    "kalshibot.research": ("kalshibot.execution",),
    "kalshibot.execution": ("kalshibot.research",),
    "kalshibot.strategies": ("kalshibot.research", "kalshibot.execution"),
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def has_commits() -> bool:
    return bool(git("rev-parse", "--verify", "-q", "HEAD").strip())


def agent_docs() -> list[Path]:
    """Every AGENTS.md git would see (respects .gitignore, so data/ and .venv/ drop out)."""
    out = git("ls-files", "--cached", "--others", "--exclude-standard", "--", "*AGENTS.md")
    return [ROOT / p for p in out.split()]


def nearest_doc(folder: Path) -> Path:
    while not (folder / "AGENTS.md").exists():
        assert folder != ROOT, "root AGENTS.md is missing"
        folder = folder.parent
    return folder / "AGENTS.md"


def map_entries(doc: Path) -> set[str]:
    return set(MAP_ROW.findall(doc.read_text()))


def test_agent_docs_are_found_everywhere():
    found = {d.relative_to(ROOT).as_posix() for d in agent_docs()}
    assert {"AGENTS.md", "src/kalshibot/data/AGENTS.md", "src/kalshibot/kalshi/AGENTS.md"} <= found


def test_every_claude_md_is_a_symlink_to_its_agents_md():
    for doc in agent_docs():
        twin = doc.with_name("CLAUDE.md")
        assert twin.is_symlink() and os.readlink(twin) == "AGENTS.md", (
            f"{twin}: must be a symlink (cd {doc.parent} && ln -sf AGENTS.md CLAUDE.md)")


def test_agent_doc_links_resolve():
    for doc in agent_docs():
        for target in LINK.findall(doc.read_text()):
            if "://" not in target:
                assert (doc.parent / target).exists(), f"{doc}: broken link {target}"


def test_every_agent_doc_is_linked_from_its_parent():
    for doc in agent_docs():
        if doc != ROOT / "AGENTS.md":
            parent = nearest_doc(doc.parent.parent)
            rel = os.path.relpath(doc, parent.parent)
            assert rel in LINK.findall(parent.read_text()), f"{parent}: no link to {rel}"


def test_every_code_file_is_in_a_map():
    """Each .py file and code directory under src/ and benchmarks/ has a map row in the
    nearest AGENTS.md above it. Directories with their own AGENTS.md are covered above."""
    code_dirs = {p.parent for top in ("src", "benchmarks") for p in (ROOT / top).rglob("*.py")}
    for folder in sorted(code_dirs):
        doc = nearest_doc(folder)
        for child in sorted(folder.iterdir()):
            if child.name == "__init__.py" or (child / "AGENTS.md").exists() or not (
                    child.suffix == ".py" or child in code_dirs):
                continue
            rel = os.path.relpath(child, doc.parent) + ("/" if child.is_dir() else "")
            assert rel in map_entries(doc), f"{doc.relative_to(ROOT)}: no map row for {rel}"


def test_map_rows_point_at_existing_paths():
    for doc in agent_docs():
        for entry in map_entries(doc):
            if "/" in entry or entry.endswith(".py"):
                assert (doc.parent / entry).exists(), f"{doc}: map row `{entry}` doesn't exist"


def test_commit_hook_strips_ai_attribution_only(tmp_path):
    assert os.access(HOOK, os.X_OK), "hook must be executable (git ignores it otherwise)"
    keep = ("Add thing\n\nRows generated with cursor pagination.\n"
            "The hook strips noreply@anthropic.com trailers.\n\n"
            "Co-authored-by: Devin Smith <devin@example.com>\n"
            "Co-authored-by: Claude Monet <claude@example.com>\n"
            "Co-authored-by: dependabot[bot] <1+dependabot[bot]@users.noreply.github.com>\n")
    drop = ("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
            "Co-developed-by: Codex <noreply@openai.com>\n"
            "Signed-off-by: Claude <noreply@anthropic.com>\n"
            "Assisted-by: Claude:claude-opus-5.5\n"
            "Co-authored-by: devin-ai-integration[bot] <1+devin-ai-integration[bot]"
            "@users.noreply.github.com>\n"
            "Co-authored-by: aider (gpt-5) <noreply@aider.chat>\n"
            "Co-authored-by: copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>\n"
            "Co-authored-by: Claude Opus 4.6 <x@y>\n"
            "Generated with Claude\n"
            "Claude-Session: https://claude.ai/code/x\n"
            "Generated with Claude Code\n"
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\n")
    msg = tmp_path / "MSG"
    msg.write_text(keep + drop)
    subprocess.run([HOOK, msg], check=True, cwd=ROOT)
    assert msg.read_text() == keep
    assert not AI_LINE.search(keep) and len(AI_LINE.findall(drop)) == 12  # test copy agrees


def test_commit_hook_refuses_ai_author(tmp_path):
    msg = tmp_path / "MSG"
    msg.write_text("Add thing\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Claude", "GIT_AUTHOR_EMAIL": "noreply@anthropic.com"}
    assert subprocess.run([HOOK, msg], cwd=ROOT, env=env, capture_output=True).returncode != 0


@pytest.mark.skipif(bool(os.getenv("CI")), reason="hooksPath is per clone; CI never commits")
def test_commit_hook_is_enabled():
    assert git("config", "core.hooksPath").strip() == ".githooks", (
        "run: git config core.hooksPath .githooks")


def test_history_has_no_ai_attribution():
    if not has_commits():
        pytest.skip("no commits yet")
    idents = git("log", "--all", "--format=%an <%ae>%n%cn <%ce>")
    assert not AI_IDENT.search(idents), AI_IDENT.search(idents)
    bodies = git("log", "--all", "--format=%B")
    assert not AI_LINE.search(bodies), AI_LINE.search(bodies)


def test_only_the_repo_gitignore_ignores_files():
    """Global/user excludes must not swallow repo files (the user's global excludes file
    lists .gitignore itself), and the repo's own rules must not swallow code or docs."""
    def ignored(*extra: str) -> set[str]:
        return set(git(*extra, "ls-files", "-oi", "--exclude-standard", "--directory").split())

    outside = ignored() - ignored("-c", "core.excludesFile=/dev/null")
    assert outside <= (set() if has_commits() else {".gitignore"}), f"ignored globally: {outside}"
    swallowed = git("ls-files", "-oi", "--exclude-standard", "--directory", "--", "src", "tests",
                    "docs", "benchmarks", ".githooks", ".github").split()
    junk = re.compile(r"(^|/)(__pycache__/|\.DS_Store$)|\.py[cod]$")
    real = [p for p in swallowed if not junk.search(p)
            and (p.endswith("/") or p.endswith((".py", ".md", ".toml", ".yml", ".txt")))]
    assert real == [], f"repo .gitignore swallows: {real}"


def test_ledgers_only_ever_grow():
    """Every ledger file ever committed still exists, and each committed version is a
    prefix of every later one and of the working copy: no trial (or unlock) can be dropped,
    which would shrink DSR's N or let the lockbox open twice."""
    ever = set(git("log", "--all", "--format=", "--name-only", "--", "ledger/").split())
    current = {p.relative_to(ROOT).as_posix() for p in (ROOT / "ledger").glob("*.jsonl")}
    for rel in sorted(ever | current):
        path = ROOT / rel
        assert path.exists(), f"{rel} was deleted"
        commits = git("log", "--reverse", "--format=%H", "--", rel).split()
        versions = [git("show", f"{c}:{rel}") for c in commits] + [path.read_text()]
        for older, newer in zip(versions, versions[1:], strict=False):
            assert newer.startswith(older), f"{rel} was rewritten, not appended to"
        if rel == "ledger/lockbox.jsonl":
            assert len(path.read_text().splitlines()) == 1, "the lockbox opens once"


def test_governance_files_are_tracked_and_env_files_are_not():
    if not has_commits():
        pytest.skip("no commits yet")
    for path in (".gitignore", ".githooks/commit-msg", ".githooks/ai-attribution.txt",
                 ".githooks/ai-identities.txt", ".claude/settings.json", "AGENTS.md", "CLAUDE.md"):
        assert git("ls-files", "--", path).strip() == path, f"{path} is not tracked (git add -f?)"
    assert git("ls-files", "--", ".env*").split() == [".env.example"]


def imported_names(source: str, package: str) -> set[str]:
    """Every module a file in `package` imports, at any depth, relative imports resolved."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0]
                base = f"{parent}.{base}" if base else parent
            names |= {base} | {f"{base}.{a.name}" for a in node.names}
    return names


def test_imported_names_resolves_nested_and_relative_imports():
    source = "def f():\n    from .. import research\n    from ..research.x import y\n"
    assert {"kalshibot.research", "kalshibot.research.x"} <= imported_names(
        source, "kalshibot.execution")


def test_runtime_dependencies_match_imports():
    import tomllib
    from importlib.metadata import packages_distributions, requires

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    specs = {canonicalize_name(r.name): r for r in map(Requirement, tomllib.loads(
        (ROOT / "pyproject.toml").read_text())["project"]["dependencies"])}
    declared = set(specs)
    dists = packages_distributions()
    used = set()
    for path in (ROOT / "src").rglob("*.py"):
        pkg = ".".join(path.relative_to(ROOT / "src").parent.parts)
        for name in imported_names(path.read_text(), pkg):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top == "kalshibot" or top not in dists:
                continue
            owners = {canonicalize_name(d) for d in dists[top]}
            assert owners & declared, (
                f"{path.relative_to(ROOT)} imports {top}, which pyproject.toml doesn't declare")
            used |= owners
    # an unimported dependency may stay only as a version cap on another's requirement
    required = {canonicalize_name(r.name) for d in declared
                for r in map(Requirement, requires(d) or [])
                if r.marker is None or r.marker.evaluate({"extra": ""})}
    unused = {d for d in declared - used if d not in required
              or not any(s.operator in ("<", "<=", "==") for s in specs[d].specifier)}
    assert not unused, f"declared but never imported: {sorted(unused)}"


@pytest.mark.parametrize("package, forbidden", LAYERS.items())
def test_layering_static(package, forbidden):
    folder = ROOT / "src" / Path(*package.split("."))
    if not folder.exists():
        pytest.skip(f"{package} doesn't exist yet")
    for path in folder.rglob("*.py"):
        pkg = ".".join(path.relative_to(ROOT / "src").parent.parts)
        bad = {n for n in imported_names(path.read_text(), pkg) for f in forbidden
               if n == f or n.startswith(f + ".")}
        assert not bad, f"{path} imports {bad}"


@pytest.mark.parametrize("package, forbidden", LAYERS.items())
def test_layering(package, forbidden):
    folder = ROOT / "src" / Path(*package.split("."))
    if not folder.exists():
        pytest.skip(f"{package} doesn't exist yet")
    modules = [".".join(p.relative_to(ROOT / "src").with_suffix("").parts).removesuffix(".__init__")
               for p in folder.rglob("*.py")]
    code = (f"import importlib, sys\nfor m in {modules!r}: importlib.import_module(m)\n"
            f"bad = {forbidden!r}\n"
            "print([m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in bad)])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", f"{package} imports {out.stdout.strip()}"
