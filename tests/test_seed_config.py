"""Tests for scripts/seed_config.py.

Seeding runs in the opposite direction to a deploy: it reads a live
instance and writes into the repo. The risk is therefore reversed too —
not breaking the house, but committing a secret or clobbering local work.
Those are the properties tested here.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import seed_config  # noqa: E402
import sync_config  # noqa: E402


@pytest.fixture
def live(tmp_path):
    """A live HA config directory, complete with things git must never see."""
    d = tmp_path / "live"
    (d / "packages").mkdir(parents=True)
    (d / ".storage").mkdir()
    (d / "deps" / "lib").mkdir(parents=True)
    (d / "www" / "community" / "mini-graph").mkdir(parents=True)
    (d / "custom_components" / "hacs").mkdir(parents=True)
    (d / "custom_components" / "mine").mkdir(parents=True)

    (d / "configuration.yaml").write_text("homeassistant:\n")
    (d / "automations.yaml").write_text("[]\n")
    (d / "packages" / "lighting.yaml").write_text("# lighting\n")
    (d / "custom_components" / "mine" / "manifest.json").write_text("{}")

    (d / "secrets.yaml").write_text("token: SUPERSECRET\n")
    (d / ".HA_VERSION").write_text("2026.8.0")
    (d / ".uuid").write_text("abc")
    (d / "home-assistant.log").write_text("noise\n")
    (d / "home-assistant_v2.db").write_text("binary")
    (d / "ip_bans.yaml").write_text("1.2.3.4\n")
    (d / ".storage" / "core.entity_registry").write_text("{}")
    (d / "deps" / "lib" / "thing.py").write_text("x=1")
    (d / "www" / "community" / "mini-graph" / "card.js").write_text("//")
    (d / "custom_components" / "hacs" / "__init__.py").write_text("#")
    return d


@pytest.fixture
def repo(tmp_path):
    return tmp_path / "repo" / "config"


def run(*argv):
    return seed_config.main(list(argv))


# --- nothing secret or machine-generated may enter the repo ----------------

@pytest.mark.parametrize("rel", [
    "secrets.yaml",
    ".storage/core.entity_registry",
    "home-assistant_v2.db",
    "home-assistant.log",
    "ip_bans.yaml",
    "known_devices.yaml",
    ".HA_VERSION",
    ".uuid",
    "deps/lib/thing.py",
    "backups/full.tar",
    "image/x.png",
    "www/community/mini-graph/card.js",
    "custom_components/hacs/__init__.py",
    "zwcfg_0x1234.xml",
    ".git/config",
    "tts/cache.mp3",
])
def test_import_denied(rel):
    assert seed_config.is_import_denied(Path(rel)), f"{rel} must not enter the repo"


@pytest.mark.parametrize("rel", [
    "configuration.yaml",
    "automations.yaml",
    "packages/lighting.yaml",
    "custom_components/mine/manifest.json",
    "dashboards/main.yaml",
    "blueprints/automation/x.yaml",
    "www/local/my-card.js",
])
def test_import_allowed(rel):
    assert not seed_config.is_import_denied(Path(rel)), f"{rel} should be imported"


def test_deploy_deny_list_is_a_subset_of_import_deny_list():
    """Anything unsafe to deploy is also unsafe to commit."""
    for pattern in sync_config.DENY:
        assert pattern in seed_config.IMPORT_DENY


def test_secret_never_written_into_repo(live, repo):
    run("--source", str(live), "--dest", str(repo), "--apply")
    assert not (repo / "secrets.yaml").exists()
    assert not (repo / ".storage").exists()
    assert not (repo / "deps").exists()
    assert not (repo / "www" / "community").exists()
    assert not (repo / "custom_components" / "hacs").exists()
    # no file anywhere in the seeded tree contains the secret
    for p in repo.rglob("*"):
        if p.is_file():
            assert "SUPERSECRET" not in p.read_text(errors="ignore")


def test_real_config_is_imported(live, repo):
    run("--source", str(live), "--dest", str(repo), "--apply")
    assert (repo / "configuration.yaml").read_text() == "homeassistant:\n"
    assert (repo / "packages" / "lighting.yaml").read_text() == "# lighting\n"
    assert (repo / "custom_components" / "mine" / "manifest.json").exists()


# --- dry run ---------------------------------------------------------------

def test_dry_run_writes_nothing(live, repo):
    assert run("--source", str(live), "--dest", str(repo)) == 0
    assert not any(repo.rglob("*.yaml"))


# --- --only scoping --------------------------------------------------------

def test_only_limits_to_subtree(live, repo):
    run("--source", str(live), "--dest", str(repo), "--only", "packages", "--apply")
    assert (repo / "packages" / "lighting.yaml").exists()
    assert not (repo / "configuration.yaml").exists()


def test_only_accepts_a_single_file(live, repo):
    run("--source", str(live), "--dest", str(repo), "--only", "automations.yaml", "--apply")
    assert (repo / "automations.yaml").exists()
    assert not (repo / "packages").exists()


def test_only_is_repeatable(live, repo):
    run("--source", str(live), "--dest", str(repo),
        "--only", "packages", "--only", "configuration.yaml", "--apply")
    assert (repo / "packages" / "lighting.yaml").exists()
    assert (repo / "configuration.yaml").exists()
    assert not (repo / "automations.yaml").exists()


def test_only_cannot_override_the_deny_list(live, repo):
    """Explicitly asking for secrets.yaml still does not import it."""
    run("--source", str(live), "--dest", str(repo), "--only", "secrets.yaml", "--apply")
    assert not (repo / "secrets.yaml").exists()


# --- never delete from the repo -------------------------------------------

def test_existing_repo_files_are_preserved(live, repo):
    repo.mkdir(parents=True)
    (repo / "packages").mkdir()
    (repo / "packages" / "handwritten.yaml").write_text("# mine\n")
    run("--source", str(live), "--dest", str(repo), "--apply")
    assert (repo / "packages" / "handwritten.yaml").read_text() == "# mine\n"


# --- wrong-source guard ----------------------------------------------------

def test_refuses_source_without_ha_marker(tmp_path, repo):
    notha = tmp_path / "notha"
    notha.mkdir()
    (notha / "readme.txt").write_text("x")
    assert run("--source", str(notha), "--dest", str(repo), "--apply") == 2
    assert not repo.exists() or not any(repo.rglob("*"))


def test_force_overrides_source_marker_check(tmp_path, repo):
    notha = tmp_path / "notha"
    notha.mkdir()
    (notha / "thing.yaml").write_text("x\n")
    assert run("--source", str(notha), "--dest", str(repo), "--apply", "--force") == 0
    assert (repo / "thing.yaml").exists()


def test_missing_source_is_an_error(tmp_path, repo):
    assert run("--source", str(tmp_path / "nope"), "--dest", str(repo)) == 2


# --- dirty-worktree guard --------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    root = tmp_path / "gitrepo"
    (root / "config").mkdir(parents=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "config" / "tracked.yaml").write_text("# committed\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def test_refuses_to_seed_over_uncommitted_changes(live, git_repo):
    dest = git_repo / "config"
    (dest / "tracked.yaml").write_text("# UNCOMMITTED EDIT\n")
    assert run("--source", str(live), "--dest", str(dest), "--apply") == 2
    assert (dest / "tracked.yaml").read_text() == "# UNCOMMITTED EDIT\n"


def test_allow_dirty_overrides_the_guard(live, git_repo):
    dest = git_repo / "config"
    (dest / "tracked.yaml").write_text("# UNCOMMITTED EDIT\n")
    assert run("--source", str(live), "--dest", str(dest),
               "--apply", "--allow-dirty") == 0
    assert (dest / "configuration.yaml").exists()


def test_clean_repo_seeds_normally(live, git_repo):
    dest = git_repo / "config"
    assert run("--source", str(live), "--dest", str(dest), "--apply") == 0
    assert (dest / "configuration.yaml").exists()


def test_dry_run_not_blocked_by_dirty_worktree(live, git_repo):
    dest = git_repo / "config"
    (dest / "tracked.yaml").write_text("# UNCOMMITTED EDIT\n")
    assert run("--source", str(live), "--dest", str(dest)) == 0


# --- classification --------------------------------------------------------

def test_changed_detected_by_content(live, repo):
    repo.mkdir(parents=True)
    (repo / "configuration.yaml").write_text("homeassistant:\n  different: true\n")
    actions, _ = seed_config.plan(live, repo, [])
    verdicts = dict((r.as_posix(), v) for r, v in actions)
    assert verdicts["configuration.yaml"] == "changed"


def test_plan_reports_skipped(live, repo):
    _, skipped = seed_config.plan(live, repo, [])
    assert Path("secrets.yaml") in skipped


# --- CLI behaviour under a closed pipe -------------------------------------

def test_piping_to_head_does_not_traceback(live, repo, tmp_path):
    """`seed_config.py ... | head -2` must not raise BrokenPipeError."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "seed_config.py"
    proc = subprocess.run(
        f'"{sys.executable}" "{script}" --source "{live}" --dest "{repo}" | head -2',
        shell=True, capture_output=True, text=True, timeout=60,
    )
    assert "BrokenPipeError" not in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr


def test_sync_piping_to_head_does_not_traceback(live, tmp_path):
    """The same guard on the deploy script."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "sync_config.py"
    target = tmp_path / "target"
    target.mkdir()
    (target / "configuration.yaml").write_text("homeassistant:\n")
    proc = subprocess.run(
        f'"{sys.executable}" "{script}" --source "{live}" --target "{target}" | head -2',
        shell=True, capture_output=True, text=True, timeout=60,
    )
    assert "BrokenPipeError" not in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
