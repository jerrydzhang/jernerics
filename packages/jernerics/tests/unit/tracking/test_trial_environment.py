import json
from pathlib import Path

from jernerics.tracking.trial_environment import TrialEnvironment


def _read_envelopes(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestSweepMetaEmission:
    def test_emits_sweep_meta_with_git_hash_and_config(self, tmp_path):
        config_src = 'base = {"lr": 0.01}\n'
        env = TrialEnvironment(
            tracking_dir=str(tmp_path),
            project_name="proj",
            study_name="study",
            trial_number=0,
            git_hash="abc123def456",
            sweep_config=config_src,
        )
        with env:
            pass

        envelopes = _read_envelopes(tmp_path / "events" / "0.jsonl")
        meta = [e for e in envelopes if "sweep_meta" in e]
        assert len(meta) == 1
        assert meta[0]["sweep_meta"]["git_hash"] == "abc123def456"
        assert meta[0]["sweep_meta"]["config"] == config_src

    def test_omits_sweep_meta_without_config(self, tmp_path):
        env = TrialEnvironment(
            tracking_dir=str(tmp_path),
            project_name="proj",
            study_name="study",
            trial_number=0,
        )
        with env:
            pass

        envelopes = _read_envelopes(tmp_path / "events" / "0.jsonl")
        assert not any("sweep_meta" in e for e in envelopes)
        # trial_end is always emitted on close
        assert any("trial_end" in e for e in envelopes)
