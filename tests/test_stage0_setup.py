"""Stage 0 gate as tests: layout, secrets hygiene, pinned installs, voice libraries, contracts."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def test_repo_layout():
    for d in ["twin", "journey", "core", "agent", "safety", "knowledge", "eval", "docs", "scripts", "tests", "db"]:
        assert (ROOT / d).is_dir(), d
    for f in ["Makefile", "pyproject.toml", "requirements.lock", ".env.example", ".gitignore", "docker-compose.yml",
              "docs/contracts.md", "scripts/check_keys.py", "db/init.sql"]:
        assert (ROOT / f).is_file(), f


@pytest.mark.parametrize("path", [".env", ".env.local", "twin.db", "journey.db-wal", "logs/x.jsonl"])
def test_secrets_and_local_data_are_never_committed(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert r.returncode == 0, f"{path} is not git-ignored"


def test_env_example_is_committable_and_has_no_values_for_secrets():
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode == 1
    for line in (ROOT / ".env.example").read_text().splitlines():
        if re.match(r"^[A-Z_]*(KEY|SECRET|TOKEN|AUTH_ID)=", line):
            assert line.endswith("="), f"a real-looking secret in .env.example: {line.split('=')[0]}"


def test_env_example_lists_every_variable_the_code_reads():
    example = set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    used: set[str] = set()
    for f in list(ROOT.glob("*/*.py")) + list(ROOT.glob("scripts/*.py")):
        if ".venv" in f.parts or "tests" in f.parts:
            continue
        for call_args in re.findall(r'(?:getenv|env|missing)\(([^)]*)\)', f.read_text()):
            used |= set(re.findall(r'"([A-Z][A-Z0-9_]+)"', call_args))
    assert used, "found no env reads at all"
    assert not used - example, f"read by code but missing from .env.example: {sorted(used - example)}"


def test_every_dependency_is_pinned():
    lines = [x.strip() for x in (ROOT / "requirements.lock").read_text().splitlines() if x.strip() and not x.startswith("#")]
    unpinned = [x for x in lines if "==" not in x]
    assert not unpinned, unpinned
    names = {x.split("==")[0].lower() for x in lines}
    for must in ["fastapi", "sqlalchemy", "anthropic", "httpx", "pytest", "livekit-agents", "livekit-plugins-sarvam",
                 "livekit-plugins-silero", "psycopg"]:
        assert must in names, must


def test_voice_libraries_import_now_not_on_day_2():
    code = "import livekit.agents, livekit.api, livekit.plugins.sarvam, livekit.plugins.silero, anthropic, psycopg"
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]


def test_contracts_doc_names_every_tool_and_twin_route():
    from journey.engine import TOOL_SPECS
    from twin.app import create_app

    doc = (ROOT / "docs/contracts.md").read_text()
    shape = lambda path: re.sub(r"\{[^}]+\}", "{}", path)  # noqa: E731  ({id} in the doc, {patient_id} in code)
    doc_paths = {shape(p) for p in re.findall(r"`(?:GET|POST|DELETE|GET/POST) (/[^ ?`]*)", doc)}
    missing_tools = [t for t in TOOL_SPECS if f"`{t}`" not in doc]
    assert not missing_tools, f"tools not in contracts.md: {missing_tools}"
    app = create_app("sqlite://", seed_on_start=False)
    routes = {r.path for r in app.routes if getattr(r, "include_in_schema", False)}
    missing_routes = sorted(p for p in routes if shape(p) not in doc_paths)
    assert not missing_routes, f"twin routes not in contracts.md: {missing_routes}"


def test_contracts_doc_names_every_journey_route():
    from journey.api import create_app
    from journey.engine import JourneyEngine

    doc = (ROOT / "docs/contracts.md").read_text()
    shape = lambda path: re.sub(r"\{[^}]+\}", "{}", path)  # noqa: E731
    doc_routes = {(m, shape(p)) for m, p in re.findall(r"`(GET|POST|DELETE) (/[^ ?`]*)", doc)}
    app = create_app(JourneyEngine("sqlite://"))
    routes = {(m, shape(r.path)) for r in app.routes if getattr(r, "include_in_schema", False) for m in r.methods}
    assert not routes - doc_routes, f"journey routes not in contracts.md: {sorted(routes - doc_routes)}"


def test_agent_and_journey_server_agree_on_the_contract_version():
    from agent.port import CONTRACT_VERSION as agent_side
    from journey.api import CONTRACT_VERSION as server_side

    assert agent_side == server_side


def _check_keys(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")}
    return subprocess.run([PY, str(ROOT / "scripts/check_keys.py"), *args], capture_output=True, text=True,
                          env=base | env, timeout=120, cwd="/")


def test_check_keys_with_nothing_set_passes_and_says_what_is_missing(monkeypatch, tmp_path):
    # run against a copy so the user's real .env (if any) is not read
    fake = tmp_path / "repo"
    (fake / "scripts").mkdir(parents=True)
    (fake / "scripts/check_keys.py").write_text((ROOT / "scripts/check_keys.py").read_text())
    r = subprocess.run([PY, str(fake / "scripts/check_keys.py")], capture_output=True, text=True, timeout=120,
                       env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")})
    assert r.returncode == 0, r.stdout + r.stderr  # Postgres is not required while the store is SQLite
    lines = [x for x in r.stdout.splitlines() if x and x[0] in "✅❌⚪"]
    assert len(lines) == 8 and all(x.startswith("⚪") for x in lines), r.stdout
    assert "No .env yet" in r.stdout


def test_check_keys_never_prints_key_values(tmp_path):
    fake = tmp_path / "repo"
    (fake / "scripts").mkdir(parents=True)
    (fake / "scripts/check_keys.py").write_text((ROOT / "scripts/check_keys.py").read_text())
    sentinel = "SENTINEL-do-not-print-7f3a"
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
           # every outbound call fails fast on a dead proxy / address instead of reaching a real service
           "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
           "NO_PROXY": "", "ANTHROPIC_API_KEY": sentinel, "SARVAM_API_KEY": sentinel,
           "LIVEKIT_URL": "wss://127.0.0.1:9", "LIVEKIT_API_KEY": sentinel, "LIVEKIT_API_SECRET": sentinel,
           "WHATSAPP_TOKEN": sentinel, "WHATSAPP_PHONE_NUMBER_ID": "1", "PLIVO_AUTH_ID": sentinel,
           "PLIVO_AUTH_TOKEN": sentinel, "DEEPGRAM_API_KEY": sentinel, "ELEVENLABS_API_KEY": sentinel,
           "JOURNEY_DB_URL": "postgresql://x", "DATABASE_URL": f"postgresql://u:{sentinel}@127.0.0.1:9/db"}
    r = subprocess.run([PY, str(fake / "scripts/check_keys.py")], capture_output=True, text=True, timeout=180, env=env)
    out = r.stdout + r.stderr
    assert sentinel not in out
    assert r.returncode == 1  # configured required services failed, and it says so
    assert out.count("❌") >= 4


def test_makefile_has_every_target_the_readme_mentions():
    make = (ROOT / "Makefile").read_text()
    readme = (ROOT / "README.md").read_text()
    for target in set(re.findall(r"make ([a-z-]+)", readme)):
        assert re.search(rf"^{target}:", make, re.M), target
