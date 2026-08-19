"""Tests for scripts/sync_config.py.

The script writes into a live Home Assistant config directory, so the
safety properties — never deploy secrets, never delete, never write during
a dry run, refuse a wrong target — are the ones worth testing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_config  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A source tree with a package, a secret, and some runtime junk."""
    src = tmp_path / "config"
    (src / "packages").mkdir(parents=True)
    (src / ".storage").mkdir()
    (src / "configuration.yaml").write_text("homeassistant:\n")
    (src / "packages" / "lighting.yaml").write_text("# lighting\n")
    (src / "secrets.yaml").write_text("token: hunter2\n")
    (src / "home-assistant.log").write_text("noise\n")
    (src / ".storage" / "core.entity_registry").write_text("{}")
    return src


@pytest.fixture
def live(tmp_path):
    """A plausible live HA config directory."""
    dst = tmp_path / "live"
    dst.mkdir()
    (dst / "configuration.yaml").write_text("homeassistant:\n")
    return dst


def run(*argv):
    return sync_config.main(list(argv))


# --- deny list -------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "secrets.yaml",
    ".storage/core.entity_registry",
    "home-assistant.log",
    "home-assistant_v2.db",
    "known_devices.yaml",
    "ip_bans.yaml",
    "packages/__pycache__/x.pyc",
    "tts/cache.mp3",
    ".env",
    "certs/server.key",
])
def test_denied_paths_are_denied(rel):
    assert sync_config.is_denied(Path(rel)), f"{rel} should never be deployed"


@pytest.mark.parametrize("rel", [
    "configuration.yaml",
    "automations.yaml",
    "packages/lighting.yaml",
    "dashboards/main.yaml",
    "secrets.example.yaml",
    "blueprints/automation/x.yaml",
])
def test_normal_config_is_not_denied(rel):
    assert not sync_config.is_denied(Path(rel)), f"{rel} should deploy"


# --- the secret must not reach the live directory --------------------------

def test_secrets_never_written(repo, live):
    run("--source", str(repo), "--target", str(live), "--apply")
    assert not (live / "secrets.yaml").exists()
    assert not (live / "home-assistant.log").exists()
    assert not (live / ".storage").exists()
    # ...while real config did land
    assert (live / "packages" / "lighting.yaml").read_text() == "# lighting\n"


def test_secret_in_live_dir_is_left_alone(repo, live):
    """A pre-existing live secrets.yaml must survive a deploy untouched."""
    (live / "secrets.yaml").write_text("token: real-live-secret\n")
    run("--source", str(repo), "--target", str(live), "--apply")
    assert (live / "secrets.yaml").read_text() == "token: real-live-secret\n"


# --- dry run ---------------------------------------------------------------

def test_dry_run_writes_nothing(repo, live):
    before = {p.name for p in live.iterdir()}
    assert run("--source", str(repo), "--target", str(live)) == 0
    assert {p.name for p in live.iterdir()} == before


# --- never delete ----------------------------------------------------------

def test_unknown_live_files_are_preserved(repo, live):
    (live / "ui-lovelace.yaml").write_text("# hand-made\n")
    run("--source", str(repo), "--target", str(live), "--apply")
    assert (live / "ui-lovelace.yaml").read_text() == "# hand-made\n"


# --- wrong-target guard ----------------------------------------------------

def test_refuses_target_without_ha_marker(repo, tmp_path):
    empty = tmp_path / "not-ha"
    empty.mkdir()
    assert run("--source", str(repo), "--target", str(empty), "--apply") == 2
    assert not (empty / "configuration.yaml").exists()


def test_force_overrides_marker_check(repo, tmp_path):
    empty = tmp_path / "not-ha"
    empty.mkdir()
    assert run("--source", str(repo), "--target", str(empty), "--apply", "--force") == 0
    assert (empty / "configuration.yaml").exists()


def test_missing_target_is_an_error(repo, tmp_path):
    assert run("--source", str(repo), "--target", str(tmp_path / "nope")) == 2


# --- backup ----------------------------------------------------------------

def test_backup_snapshots_overwritten_file(repo, live, tmp_path):
    (live / "packages").mkdir()
    (live / "packages" / "lighting.yaml").write_text("# OLD VERSION\n")
    backups = tmp_path / "backups"
    run("--source", str(repo), "--target", str(live), "--apply", "--backup", str(backups))
    snaps = list(backups.rglob("packages/lighting.yaml"))
    assert len(snaps) == 1
    assert snaps[0].read_text() == "# OLD VERSION\n"
    assert (live / "packages" / "lighting.yaml").read_text() == "# lighting\n"


def test_new_file_is_not_backed_up(repo, live, tmp_path):
    backups = tmp_path / "backups"
    run("--source", str(repo), "--target", str(live), "--apply", "--backup", str(backups))
    assert not list(backups.rglob("packages/lighting.yaml"))


# --- classification --------------------------------------------------------

def test_identical_file_classified_same_and_not_rewritten(repo, live):
    (live / "packages").mkdir()
    (live / "packages" / "lighting.yaml").write_text("# lighting\n")
    actions, _ = sync_config.plan(repo, live)
    verdicts = dict((r.as_posix(), v) for r, v in actions)
    assert verdicts["packages/lighting.yaml"] == "same"
    assert verdicts["configuration.yaml"] == "same"


def test_changed_file_detected_by_content(repo, live):
    (live / "packages").mkdir()
    (live / "packages" / "lighting.yaml").write_text("# different\n")
    actions, _ = sync_config.plan(repo, live)
    verdicts = dict((r.as_posix(), v) for r, v in actions)
    assert verdicts["packages/lighting.yaml"] == "changed"


def test_plan_reports_skipped_secrets(repo, live):
    _, skipped = sync_config.plan(repo, live)
    assert Path("secrets.yaml") in skipped
