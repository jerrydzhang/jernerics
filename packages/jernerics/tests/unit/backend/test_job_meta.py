import json

from jernerics.backend.job_meta import load_job_studies, save_job_meta


class TestSaveJobMeta:
    def test_stores_study_name(self, tmp_path):
        save_job_meta(
            job_id="123",
            remote_dir="/scratch/proj",
            n_trials=5,
            local_cache_dir=tmp_path,
            study_name="overfit_seed42",
        )
        meta = json.loads((tmp_path / "jobs" / "123.json").read_text())
        assert meta["study_name"] == "overfit_seed42"

    def test_omits_study_name_when_none(self, tmp_path):
        save_job_meta(
            job_id="456",
            remote_dir="/scratch/proj",
            n_trials=1,
            local_cache_dir=tmp_path,
        )
        meta = json.loads((tmp_path / "jobs" / "456.json").read_text())
        assert "study_name" not in meta


class TestLoadJobStudies:
    def test_returns_map_of_job_id_to_study(self, tmp_path):
        save_job_meta(
            job_id="123",
            remote_dir="/scratch/proj",
            n_trials=5,
            local_cache_dir=tmp_path,
            study_name="overfit_seed42",
        )
        assert load_job_studies(tmp_path) == {"123": "overfit_seed42"}

    def test_skips_meta_without_study_name(self, tmp_path):
        meta_dir = tmp_path / "jobs"
        meta_dir.mkdir()
        (meta_dir / "999.json").write_text(
            json.dumps({"job_id": "999", "remote_dir": "/x", "n_trials": 1})
        )
        assert load_job_studies(tmp_path) == {}

    def test_returns_empty_when_no_jobs_dir(self, tmp_path):
        assert load_job_studies(tmp_path) == {}

    def test_skips_malformed_meta(self, tmp_path):
        meta_dir = tmp_path / "jobs"
        meta_dir.mkdir()
        (meta_dir / "broken.json").write_text("not json")
        assert load_job_studies(tmp_path) == {}
