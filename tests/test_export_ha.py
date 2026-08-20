"""Tests for scripts/export_ha.py.

This script reads the one tree seed_config.py is forbidden to copy —
`.storage/`, where authored prompts sit directly alongside API keys and the
auth store. The risk it carries is therefore entirely one-directional: not
breaking the house, but committing a credential to git.

Most of what follows is that single property, approached from several angles,
plus its mirror image: the prompts themselves must survive intact, since
preserving them is the only reason the script exists.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import export_ha  # noqa: E402

# Entirely invented. Do not derive fixtures from the instance's real key —
# even its first and last characters are part of the credential.
API_KEY = "sk-or-v1-wholly000made000up000fixture000value000notreal00"
CLOUD_TOKEN = "a3f5c8e91b2d4f6a8c0e2b4d6f8a0c2e4b6d8f0a"  # 40 hex
PROMPT = (
    "You are the voice assistant for this home.\n"
    "Home is at latitude {{ state_attr('zone.home', 'latitude') }}, "
    "longitude {{ state_attr('zone.home', 'longitude') }}.\n"
    "Be brief."
)


def write_storage(root: Path, name: str, data: dict) -> None:
    d = root / ".storage"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"version": 1, "data": data}), encoding="utf-8")


@pytest.fixture
def live(tmp_path):
    """A live HA config dir whose .storage carries secrets next to prompts."""
    root = tmp_path / "live"
    root.mkdir()
    (root / "configuration.yaml").write_text("homeassistant:\n")

    write_storage(root, "core.config_entries", {"entries": [
        {
            # The dangerous shape: the *title* is the API key.
            "domain": "open_router",
            "title": API_KEY,
            "entry_id": "01ENTRY",
            "version": 1,
            "minor_version": 1,
            "created_at": 1786828405.13,
            "modified_at": 1787240880.77,
            "discovery_keys": {},
            "data": {"api_key": API_KEY},
            "options": {},
            "subentries": [
                {
                    "subentry_id": "01SUBA",
                    "subentry_type": "conversation",
                    "title": "Tier 1",
                    "unique_id": None,
                    "data": {"model": "some/model", "prompt": PROMPT,
                             "llm_hass_api": ["assist"], "web_search": "off"},
                },
            ],
        },
        {
            # No prompt anywhere: credentials only, must not be exported.
            "domain": "cloud",
            "title": "Home Assistant Cloud",
            "entry_id": "01CLOUD",
            "data": {"access_token": CLOUD_TOKEN},
            "options": {},
            "subentries": [],
        },
    ]})

    write_storage(root, "input_select", {"items": [
        {"id": "music_recall", "name": "Music Recall", "icon": "mdi:history",
         "options": ["Song A", "Song B", "Song C"]},
    ]})

    write_storage(root, "assist_pipeline.pipelines", {
        "preferred_item": "01PIPE",
        "items": [{"id": "01PIPE", "name": "Voice", "tts_voice": "en_GB-jenny",
                   "conversation_engine": "conversation.tier_one"}],
    })

    write_storage(root, "homeassistant.exposed_entities", {
        "assistants": {"conversation": {}},
        "exposed_entities": {"script.play_music": {"should_expose": True}},
    })
    return root


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "repo" / "ha_export"


def run(*argv):
    return export_ha.main(list(argv))


def all_output(live) -> str:
    return "\n".join(export_ha.render(live).values())


# --- no credential may ever reach the repo ---------------------------------

def test_api_key_never_exported(live):
    assert API_KEY not in all_output(live)


def test_secret_shaped_title_is_redacted(live):
    """The OpenRouter entry's title *is* its key — a key-name check misses it."""
    doc = yaml.safe_load(export_ha.render(live)["conversation_agents.yaml"])
    assert doc["entries"][0]["title"] == export_ha.REDACTED


def test_entry_without_a_prompt_is_not_exported_at_all(live):
    out = all_output(live)
    assert CLOUD_TOKEN not in out
    assert "01CLOUD" not in out


def test_no_secret_reaches_disk(live, dest):
    run("--source", str(live), "--dest", str(dest), "--apply")
    for path in dest.rglob("*"):
        if path.is_file():
            body = path.read_text(encoding="utf-8")
            assert API_KEY not in body
            assert CLOUD_TOKEN not in body


@pytest.mark.parametrize("value", [
    "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
    # HA titles a key-only config entry with an elided form of the key. It is
    # short and full of dots, so a length-based pattern sails past it while it
    # still discloses both ends of a live credential. Caught in a real export.
    # Shape only — never paste the instance's actual elided key here.
    "sk-or-v1-abc...xyz",
    "Bearer abc.def-ghi_jkl",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9xxxx",
    "0123456789abcdef0123456789abcdef01234567",
])
def test_credential_shapes_are_caught(value):
    assert export_ha.looks_secret(value)


@pytest.mark.parametrize("value", [
    "Anthropic: Claude Haiku 4.5",
    "en_GB-jenny_dioco-medium",
    "conversation.tier_one",
    PROMPT,
])
def test_ordinary_config_is_not_mistaken_for_a_credential(value):
    assert not export_ha.looks_secret(value)


@pytest.mark.parametrize("key", ["api_key", "latitude", "longitude", "password"])
def test_secret_keys_are_redacted_wherever_they_appear(key):
    assert export_ha.scrub({"nested": {key: "anything"}})["nested"][key] == export_ha.REDACTED


# --- the prompts must survive: preserving them is the point ----------------

def test_prompt_is_exported_verbatim(live):
    doc = yaml.safe_load(export_ha.render(live)["conversation_agents.yaml"])
    assert doc["entries"][0]["subentries"][0]["data"]["prompt"] == PROMPT


def test_coordinate_templates_in_a_prompt_are_not_redacted(live):
    """`latitude` as a key is a secret; a template that reads it is config."""
    doc = yaml.safe_load(export_ha.render(live)["conversation_agents.yaml"])
    prompt = doc["entries"][0]["subentries"][0]["data"]["prompt"]
    assert "state_attr('zone.home', 'latitude')" in prompt


def test_prompt_is_written_as_a_readable_literal_block(live):
    """Quoted style reflows the prompt and doubles apostrophes, so a one-word
    edit reads as a rewritten paragraph. Literal style keeps diffs honest."""
    text = export_ha.render(live)["conversation_agents.yaml"]
    assert "prompt: |" in text
    assert "You are the voice assistant for this home." in text
    assert "''" not in text


def test_a_string_with_trailing_whitespace_still_round_trips(live, tmp_path):
    """Literal style cannot represent trailing spaces — fall back, don't corrupt."""
    value = "line with trailing space   \nsecond line"
    dumped = yaml.dump({"prompt": value}, Dumper=export_ha._BlockDumper)
    assert yaml.safe_load(dumped)["prompt"] == value


def test_agent_wiring_is_exported(live):
    doc = yaml.safe_load(export_ha.render(live)["conversation_agents.yaml"])
    data = doc["entries"][0]["subentries"][0]["data"]
    assert data["model"] == "some/model"
    assert data["web_search"] == "off"
    assert data["llm_hass_api"] == ["assist"]


# --- diffs must stay meaningful --------------------------------------------

def test_volatile_timestamps_are_dropped(live):
    doc = yaml.safe_load(export_ha.render(live)["conversation_agents.yaml"])
    entry = doc["entries"][0]
    assert "created_at" not in entry
    assert "modified_at" not in entry


def test_input_select_values_are_excluded_but_counted(live):
    doc = yaml.safe_load(export_ha.render(live)["helpers.yaml"])
    item = doc["input_select"][0]
    assert "options" not in item
    assert item["options_count"] == 3
    assert item["name"] == "Music Recall"


def test_export_is_deterministic(live):
    assert export_ha.render(live) == export_ha.render(live)


def test_pipelines_and_exposure_are_exported(live):
    rendered = export_ha.render(live)
    pipelines = yaml.safe_load(rendered["pipelines.yaml"])
    assert pipelines["preferred_pipeline"] == "01PIPE"
    assert pipelines["pipelines"][0]["tts_voice"] == "en_GB-jenny"
    exposure = yaml.safe_load(rendered["exposed_entities.yaml"])
    assert exposure["exposed_entities"]["script.play_music"]["should_expose"] is True


def test_every_file_carries_the_not_a_deploy_source_header(live):
    for text in export_ha.render(live).values():
        assert "NOT deployed by sync_config.py" in text


# --- CLI behaviour ---------------------------------------------------------

def test_dry_run_writes_nothing(live, dest):
    assert run("--source", str(live), "--dest", str(dest)) == 0
    assert not dest.exists()


def test_apply_writes_the_expected_files(live, dest):
    assert run("--source", str(live), "--dest", str(dest), "--apply") == 0
    written = {p.name for p in dest.iterdir()}
    assert written == {"conversation_agents.yaml", "helpers.yaml",
                       "pipelines.yaml", "exposed_entities.yaml"}


def test_rerun_is_idempotent(live, dest):
    run("--source", str(live), "--dest", str(dest), "--apply")
    before = {p.name: p.read_text(encoding="utf-8") for p in dest.iterdir()}
    run("--source", str(live), "--dest", str(dest), "--apply")
    after = {p.name: p.read_text(encoding="utf-8") for p in dest.iterdir()}
    assert before == after


def test_refuses_source_without_ha_marker(tmp_path, dest):
    notha = tmp_path / "notha"
    (notha / ".storage").mkdir(parents=True)
    assert run("--source", str(notha), "--dest", str(dest)) == 2


def test_missing_source_is_an_error(tmp_path, dest):
    assert run("--source", str(tmp_path / "nope"), "--dest", str(dest)) == 2


def test_source_without_storage_reports_nothing_to_export(tmp_path, dest):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "configuration.yaml").write_text("homeassistant:\n")
    assert run("--source", str(empty), "--dest", str(dest)) == 1


def test_piping_to_head_does_not_traceback(live, dest):
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_ha.py"
    proc = subprocess.run(
        f'"{sys.executable}" "{script}" --source "{live}" --dest "{dest}" | head -2',
        shell=True, capture_output=True, text=True, timeout=60,
    )
    assert "BrokenPipeError" not in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr


# --- the output tree must never become a deploy source ---------------------

def test_export_dest_is_outside_the_deployed_config_tree():
    """sync_config deploys `config/`; this must not default inside it."""
    import sync_config
    default_deploy_source = Path("config")
    default_export_dest = Path("ha_export")
    assert default_export_dest != default_deploy_source
    assert default_deploy_source not in default_export_dest.parents
    assert not sync_config.matches(default_export_dest, sync_config.DENY)
