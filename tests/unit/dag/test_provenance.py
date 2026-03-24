import json
from pathlib import Path

from jernerics.dag.provenance import (
    Provenance,
    _get_file_hash,
    _get_git_sha,
    _get_slurm_job_id,
    _resolve_container_path,
)


class TestGitSha:
    def test_get_git_sha_returns_string(self):
        result = _get_git_sha(Path("."))
        assert result is None or isinstance(result, str)
        if result:
            assert len(result) == 8

    def test_get_git_sha_nonexistent_dir(self):
        result = _get_git_sha(Path("/nonexistent/path"))
        assert result is None

    def test_get_git_sha_file_not_dir(self, tmp_path):
        file_path = tmp_path / "notadir.txt"
        file_path.write_text("content")

        result = _get_git_sha(file_path)
        assert result is None

    def test_get_git_sha_none_path(self):
        result = _get_git_sha(None)
        assert result is None


class TestFileHash:
    def test_get_file_hash_existing_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = _get_file_hash(test_file)

        assert result is not None
        assert result.startswith("sha256:")
        assert len(result) == 71  # "sha256:" + 64 hex chars

    def test_get_file_hash_nonexistent_file(self):
        result = _get_file_hash(Path("/nonexistent/file.txt"))
        assert result is None

    def test_get_file_hash_consistent(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result1 = _get_file_hash(test_file)
        result2 = _get_file_hash(test_file)

        assert result1 == result2


class TestResolveContainerPath:
    def test_resolve_none(self):
        assert _resolve_container_path(None) is None

    def test_resolve_nonexistent_path(self):
        result = _resolve_container_path("/nonexistent/container.sif")
        assert result == {"path": "/nonexistent/container.sif"}

    def test_resolve_regular_path(self, tmp_path):
        container = tmp_path / "container.sif"
        container.touch()

        result = _resolve_container_path(str(container))

        assert "path" in result
        assert "store_path" not in result


class TestSlurmJobId:
    def test_get_slurm_job_id_not_set(self, monkeypatch):
        monkeypatch.delenv("SLURM_JOB_ID", raising=False)
        assert _get_slurm_job_id() is None

    def test_get_slurm_job_id_set(self, monkeypatch):
        monkeypatch.setenv("SLURM_JOB_ID", "12345")
        assert _get_slurm_job_id() == "12345"


class TestProvenance:
    def test_create_basic(self):
        prov = Provenance.create(run_id="test-123")

        assert prov.run_id == "test-123"
        assert prov.jernerics_version != "unknown"
        assert prov.started_at is not None
        assert prov.ended_at is None
        assert prov.config == {}
        assert prov.container is None
        assert prov.slurm_job_id is None

    def test_create_with_config_path(self, tmp_path):
        config_file = tmp_path / "config.py"
        config_file.write_text("configs = [{'lr': 0.001}]")

        prov = Provenance.create(
            run_id="test-123",
            config_path=str(config_file),
        )

        assert prov.config["path"] == str(config_file)
        assert prov.config["hash"].startswith("sha256:")

    def test_create_with_container_path(self, tmp_path):
        container = tmp_path / "container.sif"
        container.touch()

        prov = Provenance.create(
            run_id="test-123",
            container_path=str(container),
        )

        assert prov.container is not None
        assert "path" in prov.container

    def test_create_with_slurm_job(self, monkeypatch):
        monkeypatch.setenv("SLURM_JOB_ID", "99999")

        prov = Provenance.create(run_id="test-123")

        assert prov.slurm_job_id == "99999"

    def test_finalize(self):
        prov = Provenance.create(run_id="test-123")
        assert prov.ended_at is None

        prov.finalize()

        assert prov.ended_at is not None

    def test_to_dict(self):
        prov = Provenance.create(run_id="test-123")
        prov.finalize()

        result = prov.to_dict()

        assert isinstance(result, dict)
        assert result["run_id"] == "test-123"
        assert "jernerics_version" in result
        assert "started_at" in result
        assert "ended_at" in result

    def test_to_json(self, tmp_path):
        prov = Provenance.create(run_id="test-123")
        prov.finalize()

        result_path = prov.to_json(tmp_path)

        assert result_path.exists()
        assert result_path.name == "test-123_provenance.json"

        with open(result_path) as f:
            data = json.load(f)

        assert data["run_id"] == "test-123"

    def test_from_json(self, tmp_path):
        original = Provenance.create(
            run_id="test-456",
            config_path=None,
            container_path=None,
        )
        original.finalize()

        json_path = original.to_json(tmp_path)
        loaded = Provenance.from_json(json_path)

        assert loaded.run_id == original.run_id
        assert loaded.jernerics_version == original.jernerics_version
        assert loaded.git_sha == original.git_sha
        assert loaded.started_at == original.started_at
        assert loaded.ended_at == original.ended_at
