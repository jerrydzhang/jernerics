from unittest.mock import MagicMock, patch

import pytest

from jernerics.hpc.slurm import SlurmJob, SlurmJobManager


class TestSlurmJob:
    def test_slurm_job_creation(self):
        job = SlurmJob(
            job_id="12345",
            name="test_job",
            status="RUNNING",
            partition="gpu",
            time="1:00:00",
            nodes="node01",
        )
        assert job.job_id == "12345"
        assert job.name == "test_job"
        assert job.status == "RUNNING"
        assert job.partition == "gpu"
        assert job.time == "1:00:00"
        assert job.nodes == "node01"


class TestSlurmJobManager:
    def test_submit_calls_sbatch(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(stdout="12345\n", returncode=0)

        manager = SlurmJobManager(mock_ssh)
        job_id = manager.submit("/path/to/script.sh")

        assert job_id == "12345"
        mock_ssh.run.assert_called_once()
        assert "sbatch" in mock_ssh.run.call_args[0][0]

    def test_submit_inline_with_workdir(self):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@hpc.example.edu"

        manager = SlurmJobManager(mock_ssh)

        with patch("jernerics.hpc.slurm.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="67890\n", returncode=0)
            job_id = manager.submit_inline("script content", workdir="~/project")

            assert job_id == "67890"
            mock_run.assert_called_once()
            args, _kwargs = mock_run.call_args
            assert args[0] == [
                "ssh",
                "user@hpc.example.edu",
                "cd ~/project && sbatch --parsable",
            ]

    def test_submit_inline_without_workdir(self):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@hpc.example.edu"

        manager = SlurmJobManager(mock_ssh)

        with patch("jernerics.hpc.slurm.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="67890\n", returncode=0)
            job_id = manager.submit_inline("script content")

            assert job_id == "67890"
            args, _kwargs = mock_run.call_args
            assert args[0] == ["ssh", "user@hpc.example.edu", "sbatch --parsable"]

    def test_submit_inline_raises_on_failure(self):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@hpc.example.edu"

        manager = SlurmJobManager(mock_ssh)

        with patch("jernerics.hpc.slurm.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="", stderr="sbatch: error", returncode=1
            )

            with pytest.raises(RuntimeError, match="Failed to submit job"):
                manager.submit_inline("script content")

    def test_list_jobs_parses_output(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(
            stdout="JOBID|NAME|STATE|PARTITION|TIME|NODE\n12345|test|RUNNING|gpu|1:00|node01\n",
            returncode=0,
        )

        manager = SlurmJobManager(mock_ssh)
        jobs = manager.list_jobs()

        assert len(jobs) == 1
        assert jobs[0].job_id == "12345"
        assert jobs[0].name == "test"
        assert jobs[0].status == "RUNNING"

    def test_list_jobs_empty(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(
            stdout="JOBID|NAME|STATE|PARTITION|TIME|NODE\n", returncode=0
        )

        manager = SlurmJobManager(mock_ssh)
        jobs = manager.list_jobs()

        assert len(jobs) == 0

    def test_cancel_job(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(returncode=0)

        manager = SlurmJobManager(mock_ssh)
        result = manager.cancel("12345")

        assert result is True
        mock_ssh.run.assert_called_once_with("scancel 12345", check=False)

    def test_cancel_all(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(returncode=0)

        manager = SlurmJobManager(mock_ssh)
        result = manager.cancel_all()

        assert result is True
        mock_ssh.run.assert_called_once_with("scancel -u $USER", check=False)

    def test_get_status_running(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(stdout="RUNNING\n", returncode=0)

        manager = SlurmJobManager(mock_ssh)
        status = manager.get_status("12345")

        assert status == "RUNNING"

    def test_get_status_not_found(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(stdout="", returncode=1)

        manager = SlurmJobManager(mock_ssh)
        status = manager.get_status("99999")

        assert status is None

    def test_get_job_output_path_replaces_percent_j(self):
        mock_ssh = MagicMock()
        manager = SlurmJobManager(mock_ssh)

        result = manager.get_job_output_path("12345", "slurm_%j.out")
        assert result == "slurm_12345.out"

    def test_get_job_output_path_replaces_percent_A(self):
        mock_ssh = MagicMock()
        manager = SlurmJobManager(mock_ssh)

        result = manager.get_job_output_path("12345", "slurm_%A_%a.out")
        assert result == "slurm_12345_%a.out"
