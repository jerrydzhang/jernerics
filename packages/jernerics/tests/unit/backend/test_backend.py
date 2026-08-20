import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.backend import Backend
from jernerics.backend.container import NoContainer
from jernerics.backend.host import LocalHost
from jernerics.backend.models import JobSubmission, SubmitResult, SweepSubmission
from jernerics.backend.path_resolver import PathResolver
from jernerics.backend.submission import SweepInfrastructure


def _make_backend(host=None, container=None, adapter=None, syncer=None, **overrides):
    if host is None:
        host = MagicMock()
        host.home = "/home/user"
        host.run.return_value = MagicMock(returncode=0, stdout="")
    if container is None:
        container = NoContainer()
    if adapter is None:
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
    defaults = {
        "project_name": "proj",
        "tracking_server": None,
        "heartbeat_interval_s": 60.0,
        "remote_dir": "/scratch/user/proj",
        "cache_host": "/scratch/cache",
    }
    defaults.update(overrides)
    paths = PathResolver(
        remote_dir=defaults["remote_dir"],
        cache_dir=defaults["cache_host"],
        container=container,
        project_name=defaults["project_name"],
    )
    infra = SweepInfrastructure(adapter=adapter, container=container, paths=paths)
    return Backend(
        host=host,
        infra=infra,
        syncer=syncer,
        project_name=defaults["project_name"],
        tracking_server=defaults["tracking_server"],
        heartbeat_interval_s=defaults["heartbeat_interval_s"],
    )


def _make_spec(
    trial_path=Path("trial.py"),
    config_path=Path("config.py"),
    study_name="mystudy",
    storage_url="sqlite:////cache/optuna/mystudy.journal",
    n_trials=5,
    trial_relpath="trial.py",
    config_relpath="config.py",
    project_name="proj",
):
    return SweepSubmission(
        trial_path=trial_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_url,
        n_trials=n_trials,
        trial_relpath=trial_relpath,
        config_relpath=config_relpath,
        project_name=project_name,
    )


class TestPrepareAndSubmit:
    def test_builds_params_and_delegates_to_adapter(self):
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="999", n_trials=5)]
        )
        backend = _make_backend(adapter=adapter)
        spec = _make_spec()

        result = backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            dry_run=False,
            backend_name="hpc",
        )

        adapter.submit_sweep.assert_called_once()
        params = adapter.submit_sweep.call_args[0][0]
        assert isinstance(params, SweepSubmissionParams)
        assert params.n_trials == 5
        assert params.study_name == "mystudy"
        assert result.submissions[0].job_id == "999"

    def test_dry_run_calls_render_not_submit(self):
        adapter = MagicMock()
        adapter.render_sweep.return_value = "#!/bin/bash\necho hello"
        backend = _make_backend(adapter=adapter)
        spec = _make_spec()

        result = backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            dry_run=True,
            backend_name="hpc",
        )

        adapter.render_sweep.assert_called_once()
        adapter.submit_sweep.assert_not_called()
        assert result is None

    def test_syncs_project_before_submit(self):
        host = MagicMock()
        host.home = "/home/user"
        host.run.return_value = MagicMock(returncode=0, stdout="")
        syncer = MagicMock()
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        backend = _make_backend(
            host=host,
            adapter=adapter,
            syncer=syncer,
        )
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
        )

        syncer.sync_project.assert_called_once_with(Path("/home/user/proj"))

    def test_saves_job_meta(self, tmp_path):
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        backend = _make_backend(adapter=adapter)
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
            local_cache_dir=tmp_path,
        )

        meta_file = tmp_path / "jobs" / "123.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["n_trials"] == 5
        assert meta["study_name"] == "mystudy"

    @patch("jernerics.backend.backend.resolve_tracking_ship")
    @patch("jernerics.backend.backend.ship_events_file")
    def test_ships_submission_events_after_submit(
        self, mock_ship, mock_resolve, tmp_path
    ):
        mock_resolve.return_value = ("http://localhost:8000", None)
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        backend = _make_backend(
            host=LocalHost(),
            adapter=adapter,
            tracking_server="http://localhost:8000",
            remote_dir=str(tmp_path / "proj"),
            cache_host=str(tmp_path / "cache"),
        )
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=tmp_path / "proj",
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
        )

        adapter.submit_sweep.assert_called_once()
        mock_ship.assert_called_once()
        path, base_url = mock_ship.call_args[0][:2]
        assert path == (
            tmp_path
            / "cache"
            / "proj"
            / "tracking"
            / "mystudy"
            / f"{spec.submission_id}.jsonl"
        )
        assert base_url == "http://localhost:8000"

    @patch("jernerics.backend.backend.resolve_tracking_ship")
    @patch("jernerics.backend.backend.ship_events_file")
    def test_shipping_failure_does_not_fail_submission(
        self, mock_ship, mock_resolve, tmp_path
    ):
        mock_resolve.return_value = ("http://localhost:8000", None)
        mock_ship.return_value = False
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        backend = _make_backend(
            host=LocalHost(),
            adapter=adapter,
            tracking_server="http://localhost:8000",
            remote_dir=str(tmp_path / "proj"),
            cache_host=str(tmp_path / "cache"),
        )
        spec = _make_spec()

        result = backend.prepare_and_submit(
            spec,
            project_dir=tmp_path / "proj",
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
        )

        assert result.submissions[0].job_id == "123"

    @patch("jernerics.backend.backend.ship_events_file")
    def test_no_ship_without_tracking_server(self, mock_ship):
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        backend = _make_backend(adapter=adapter)
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=Path("/tmp/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
        )

        mock_ship.assert_not_called()

    def test_constructs_post_hook_with_retry(self, tmp_path):
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[
                JobSubmission(job_id="100", n_trials=5),
                JobSubmission(job_id="101", n_trials=0),
            ]
        )
        host = MagicMock()
        host.home = "/home/user"
        host.run.return_value = MagicMock(returncode=0, stdout="")
        backend = _make_backend(
            host=host,
            adapter=adapter,
        )
        spec = _make_spec()

        result = backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
            local_cache_dir=tmp_path,
        )

        adapter.submit_sweep.assert_called_once()
        params = adapter.submit_sweep.call_args[0][0]
        assert params.post_hook_command is not None
        assert len(result.submissions) == 2

    def test_always_writes_retry_context_to_host(self, tmp_path):
        host = MagicMock()
        host.home = "/home/user"
        host.run.return_value = MagicMock(returncode=0, stdout="")
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="100", n_trials=5)]
        )
        backend = _make_backend(host=host, adapter=adapter)
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
            local_cache_dir=tmp_path,
        )

        # Verify retry context was written to host (host.write_file called)
        write_calls = [
            c for c in host.write_file.call_args_list if "_ctx.json" in str(c)
        ]
        assert len(write_calls) == 1

        # Verify ctx file contains valid JSON with study_name
        import json

        ctx_content = write_calls[0][0][1]
        ctx = json.loads(ctx_content)
        assert ctx["study_name"] == "mystudy"

        # Verify post_hook_command is present
        params = adapter.submit_sweep.call_args[0][0]
        assert params.post_hook_command is not None

    def test_always_constructs_post_hook(self, tmp_path):
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="100", n_trials=5)]
        )
        backend = _make_backend(adapter=adapter)
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
            local_cache_dir=tmp_path,
        )

        params = adapter.submit_sweep.call_args[0][0]
        assert params.post_hook_command is not None
        assert "python -m jernerics.post_hook" in params.post_hook_command


class TestBuild:
    def test_delegates_to_adapter_submit_job(self, tmp_path):
        adapter = MagicMock()
        adapter.submit_job.return_value = "456"
        container = MagicMock()
        container.build_command.return_value = ["docker", "build", "-t", "img", "."]
        backend = _make_backend(adapter=adapter, container=container)

        (tmp_path / "uv.lock").write_text("")
        cache = tmp_path / "cache"
        cache.mkdir()

        backend.build(
            tmp_path,
            project_name="proj",
            force=True,
            local_cache_dir=cache,
        )

        adapter.submit_job.assert_called_once()

    def test_saves_meta_after_build(self, tmp_path):
        adapter = MagicMock()
        adapter.submit_job.return_value = "789"
        container = MagicMock()
        container.build_command.return_value = ["docker", "build", "-t", "img", "."]
        backend = _make_backend(
            adapter=adapter,
            container=container,
        )

        (tmp_path / "uv.lock").write_text("")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        backend.build(
            tmp_path,
            project_name="proj",
            force=True,
            local_cache_dir=cache_dir,
        )

        meta_file = cache_dir / "jobs" / "789.json"
        assert meta_file.exists()


class TestPathDependencyCheck:
    def _write_pyproject(self, tmp_path, sources_body=""):
        (tmp_path / "uv.lock").write_text("")
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "proj"\n'
            'version = "0.1.0"\n'
            f"\n[tool.uv.sources]\n{sources_body}"
        )

    def test_raises_on_path_dependency(self, tmp_path):
        self._write_pyproject(
            tmp_path,
            'jernerics = { path = "../jernerics/packages/jernerics", '
            "editable = true }\n",
        )
        backend = _make_backend()

        with pytest.raises(
            RuntimeError, match="Path dependencies detected"
        ) as exc_info:
            backend.build(tmp_path, project_name="proj", force=True)

        msg = str(exc_info.value)
        assert 'jernerics -> "../jernerics/packages/jernerics"' in msg or (
            "jernerics -> ../jernerics/packages/jernerics" in msg
        )
        assert "git dependency" in msg
        assert "[tool.uv.sources.jernerics]" in msg

    def test_multiple_path_deps_all_listed(self, tmp_path):
        self._write_pyproject(
            tmp_path,
            'jernerics = { path = "../jernerics/packages/jernerics" }\n'
            "jernerics-server = { path = "
            '"../jernerics/packages/jernerics-server" }\n',
        )
        backend = _make_backend()

        with pytest.raises(
            RuntimeError, match="Path dependencies detected"
        ) as exc_info:
            backend.build(tmp_path, project_name="proj", force=True)

        msg = str(exc_info.value)
        assert "jernerics -> " in msg
        assert "jernerics-server -> " in msg

    def test_git_source_does_not_raise(self, tmp_path):
        self._write_pyproject(
            tmp_path,
            "[tool.uv.sources.jernerics]\n"
            'git = "https://github.com/jerrydzhang/jernerics.git"\n'
            'branch = "main"\n'
            'subdirectory = "packages/jernerics"\n',
        )
        adapter = MagicMock()
        adapter.submit_job.return_value = "456"
        container = MagicMock()
        container.build_command.return_value = ["docker", "build", "-t", "img", "."]
        backend = _make_backend(adapter=adapter, container=container)

        backend.build(tmp_path, project_name="proj", force=True)

        adapter.submit_job.assert_called_once()

    def test_index_source_does_not_raise(self, tmp_path):
        self._write_pyproject(
            tmp_path,
            'torch = { index = "pytorch-cu124" }\n',
        )
        adapter = MagicMock()
        adapter.submit_job.return_value = "456"
        container = MagicMock()
        container.build_command.return_value = ["docker", "build", "-t", "img", "."]
        backend = _make_backend(adapter=adapter, container=container)

        backend.build(tmp_path, project_name="proj", force=True)

        adapter.submit_job.assert_called_once()

    def test_no_pyproject_does_not_raise(self, tmp_path):
        (tmp_path / "uv.lock").write_text("")
        adapter = MagicMock()
        adapter.submit_job.return_value = "456"
        container = MagicMock()
        container.build_command.return_value = ["docker", "build", "-t", "img", "."]
        backend = _make_backend(adapter=adapter, container=container)

        backend.build(tmp_path, project_name="proj", force=True)

        adapter.submit_job.assert_called_once()

    def test_no_sources_section_does_not_raise(self, tmp_path):
        (tmp_path / "uv.lock").write_text("")
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'proj'\nversion = '0.1.0'\n"
        )
        adapter = MagicMock()
        adapter.submit_job.return_value = "456"
        container = MagicMock()
        container.build_command.return_value = ["docker", "build", "-t", "img", "."]
        backend = _make_backend(adapter=adapter, container=container)

        backend.build(tmp_path, project_name="proj", force=True)

        adapter.submit_job.assert_called_once()


class TestDelegatedMethods:
    def test_list_jobs(self):
        from jernerics.backend.models import JobInfo

        adapter = MagicMock()
        adapter.list_jobs.return_value = [
            JobInfo(job_id="1", name="job", status="RUNNING")
        ]
        backend = _make_backend(adapter=adapter)

        jobs = backend.list_jobs()
        assert len(jobs) == 1
        adapter.list_jobs.assert_called_once()

    def test_list_jobs_enriches_study_name(self, tmp_path):
        from jernerics.backend.job_meta import save_job_meta
        from jernerics.backend.models import JobInfo

        save_job_meta(
            job_id="1",
            remote_dir="/scratch/proj",
            n_trials=5,
            local_cache_dir=tmp_path,
            study_name="overfit_seed42",
        )
        adapter = MagicMock()
        adapter.list_jobs.return_value = [
            JobInfo(job_id="1", name="job", status="RUNNING")
        ]
        backend = _make_backend(adapter=adapter)

        jobs = backend.list_jobs(local_cache_dir=tmp_path)
        assert jobs[0].study_name == "overfit_seed42"

    def test_list_jobs_resolves_array_task_id(self, tmp_path):
        from jernerics.backend.job_meta import save_job_meta
        from jernerics.backend.models import JobInfo

        save_job_meta(
            job_id="100",
            remote_dir="/scratch/proj",
            n_trials=5,
            local_cache_dir=tmp_path,
            study_name="overfit_seed42",
        )
        adapter = MagicMock()
        adapter.list_jobs.return_value = [
            JobInfo(job_id="100_3", name="job", status="RUNNING")
        ]
        backend = _make_backend(adapter=adapter)

        jobs = backend.list_jobs(local_cache_dir=tmp_path)
        assert jobs[0].study_name == "overfit_seed42"

    def test_list_jobs_without_cache_dir_leaves_study_blank(self):
        from jernerics.backend.models import JobInfo

        adapter = MagicMock()
        adapter.list_jobs.return_value = [
            JobInfo(job_id="1", name="job", status="RUNNING")
        ]
        backend = _make_backend(adapter=adapter)

        jobs = backend.list_jobs()
        assert jobs[0].study_name == ""

    def test_cancel(self):
        adapter = MagicMock()
        adapter.cancel.return_value = True
        backend = _make_backend(adapter=adapter)

        assert backend.cancel("123") is True
        adapter.cancel.assert_called_once_with("123")

    def test_cancel_all(self):
        adapter = MagicMock()
        adapter.cancel_all.return_value = True
        backend = _make_backend(adapter=adapter)

        assert backend.cancel_all() is True

    def test_get_status(self):
        adapter = MagicMock()
        adapter.get_status.return_value = "RUNNING"
        backend = _make_backend(adapter=adapter)

        assert backend.get_status("123") == "RUNNING"

    def test_wait_for_completion(self):
        adapter = MagicMock()
        adapter.wait_for_completion.return_value = True
        backend = _make_backend(adapter=adapter)

        assert backend.wait_for_completion("123") is True

    def test_get_logs(self):
        adapter = MagicMock()
        backend = _make_backend(adapter=adapter)

        backend.get_logs("123", local_cache_dir=Path("/cache"))
        adapter.get_logs.assert_called_once()

    def test_cleanup(self):
        adapter = MagicMock()
        backend = _make_backend(adapter=adapter)

        backend.cleanup()
        adapter.cleanup.assert_called_once()


class TestStoragePath:
    def test_delegates_to_paths(self):
        backend = _make_backend()
        assert backend.storage_path("mystudy") is not None


class TestSync:
    def test_raises_without_tracking_server(self):
        backend = _make_backend(tracking_server=None)
        with pytest.raises(RuntimeError, match="tracking server"):
            backend.sync("proj")


class TestPrepareAndSubmitOverrides:
    def test_merges_overrides_into_params(self):
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        backend = _make_backend(adapter=adapter)
        spec = _make_spec()

        backend.prepare_and_submit(
            spec,
            project_dir=Path("/home/user/proj"),
            project_name="proj",
            direction="minimize",
            backend_name="hpc",
            experiment_overrides={"partition": "gpu", "time": "4:00:00"},
            cli_overrides={"mem": "32G"},
        )

        params = adapter.submit_sweep.call_args[0][0]
        assert params.overrides["partition"] == "gpu"
        assert params.overrides["time"] == "4:00:00"
        assert params.overrides["mem"] == "32G"
