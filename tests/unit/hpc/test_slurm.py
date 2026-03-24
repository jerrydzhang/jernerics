from unittest.mock import MagicMock

import pytest

from jernerics.hpc.slurm import SlurmJob, SlurmJobManager, expand_slurm_pattern


class TestExpandSlurmPattern:
    def test_replaces_percent_j(self):
        result = expand_slurm_pattern("slurm_%j.out", job_id="12345")
        assert result == "slurm_12345.out"

    def test_replaces_percent_A(self):
        result = expand_slurm_pattern("slurm_%A.out", job_id="12345_1")
        assert result == "slurm_12345.out"

    def test_replaces_percent_a(self):
        result = expand_slurm_pattern("slurm_%a.out", array_task_id=5)
        assert result == "slurm_5.out"

    def test_replaces_percent_x(self):
        result = expand_slurm_pattern("slurm_%x.out", job_name="myjob")
        assert result == "slurm_myjob.out"

    def test_replaces_percent_u(self):
        import os

        result = expand_slurm_pattern("slurm_%u.out")
        assert result == f"slurm_{os.environ.get('USER', 'unknown')}.out"

    def test_wildcard_for_unknown_array(self):
        result = expand_slurm_pattern(
            "slurm_%a.out", replace_unknown_with_wildcard=True
        )
        assert result == "slurm_*.out"

    def test_no_wildcard_without_flag(self):
        result = expand_slurm_pattern("slurm_%a.out")
        assert result == "slurm_%a.out"

    def test_wildcard_for_percent_N(self):
        result = expand_slurm_pattern(
            "slurm_%N.out", replace_unknown_with_wildcard=True
        )
        assert result == "slurm_*.out"

    def test_multiple_patterns(self):
        result = expand_slurm_pattern(
            "%x_%j_%a.out", job_id="12345", array_task_id=1, job_name="test"
        )
        assert result == "test_12345_1.out"


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
        mock_ssh.run.return_value = MagicMock(stdout="67890\n", returncode=0)

        manager = SlurmJobManager(mock_ssh)
        job_id = manager.submit_inline("script content", workdir="~/project")

        assert job_id == "67890"
        mock_ssh.run.assert_called_once()
        args, kwargs = mock_ssh.run.call_args
        assert "cd ~/project && sbatch --parsable" == args[0]
        assert kwargs.get("input") == "script content"
        assert kwargs.get("check") is False

    def test_submit_inline_without_workdir(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(stdout="67890\n", returncode=0)

        manager = SlurmJobManager(mock_ssh)
        job_id = manager.submit_inline("script content")

        assert job_id == "67890"
        args, kwargs = mock_ssh.run.call_args
        assert args[0] == "sbatch --parsable"
        assert kwargs.get("input") == "script content"
        assert kwargs.get("check") is False

    def test_submit_inline_raises_on_failure(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(
            stdout="", stderr="sbatch: error", returncode=1
        )

        manager = SlurmJobManager(mock_ssh)

        with pytest.raises(RuntimeError, match="Failed to submit job"):
            manager.submit_inline("script content")

    def test_list_jobs_parses_output(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(
            stdout="JOBID\tNAME\tSTATE\tPARTITION\tTIME\tNODE\n12345\ttest\tRUNNING\tgpu\t1:00\tnode01\n",
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
            stdout="JOBID\tNAME\tSTATE\tPARTITION\tTIME\tNODE\n", returncode=0
        )

        manager = SlurmJobManager(mock_ssh)
        jobs = manager.list_jobs()

        assert len(jobs) == 0

    def test_list_jobs_with_pipe_in_name(self):
        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(
            stdout="JOBID\tNAME\tSTATE\tPARTITION\tTIME\tNODE\n12345\texperiment|v2\tRUNNING\tgpu\t1:00\tnode01\n",
            returncode=0,
        )

        manager = SlurmJobManager(mock_ssh)
        jobs = manager.list_jobs()

        assert len(jobs) == 1
        assert jobs[0].name == "experiment|v2"

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

    def test_get_job_output_path_with_job_name(self):
        mock_ssh = MagicMock()
        manager = SlurmJobManager(mock_ssh)

        result = manager.get_job_output_path(
            "12345", "slurm_%j_%x.out", job_name="myjob"
        )
        assert result == "slurm_12345_myjob.out"

    def test_get_job_output_path_with_array_task_id(self):
        mock_ssh = MagicMock()
        manager = SlurmJobManager(mock_ssh)

        result = manager.get_job_output_path(
            "12345", "slurm_%A_%a.out", array_task_id=3
        )
        assert result == "slurm_12345_3.out"

    def test_get_job_output_path_with_wildcard_replacement(self):
        mock_ssh = MagicMock()
        manager = SlurmJobManager(mock_ssh)

        result = manager.get_job_output_path(
            "12345", "slurm_%j_%a_%x.out", replace_unknown_with_wildcard=True
        )
        assert result == "slurm_12345_*_*.out"
