"""Tests for PueueAdapter."""

import json
from unittest.mock import MagicMock

import pytest
from jernerics.backend.adapter import SchedulerAdapter
from jernerics.backend.pueue.adapter import PueueAdapter
from jernerics.config import BackendConfig, PueueConfig, SharedConfig


def _make_adapter(host=None, **overrides):
    defaults = {
        "remote_dir": "$HOME/projects/proj",
        "cache_dir": "$HOME/.cache/jernerics",
        "parallel": 2,
    }
    defaults.update(overrides)
    if host is None:
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="0")
    return PueueAdapter(host=host, **defaults)


def _make_params(
    setup_command="wrapped_setup",
    trial_command="wrapped_trial",
    n_trials=3,
    study_name="mystudy",
    log_dir="/cache/logs",
    post_hook_command=None,
    max_parallel=None,
    overrides=None,
):
    from jernerics.backend.adapter import SweepSubmissionParams

    return SweepSubmissionParams(
        setup_command=setup_command,
        trial_command=trial_command,
        n_trials=n_trials,
        study_name=study_name,
        log_dir=log_dir,
        post_hook_command=post_hook_command,
        max_parallel=max_parallel,
        overrides=overrides or {},
    )


class TestRenderSweep:
    def test_creates_pueue_group_and_parallel(self):
        adapter = _make_adapter(parallel=4)
        params = _make_params(n_trials=5)
        script = adapter.render_sweep(params)

        assert "pueue group add mystudy" in script
        assert "pueue parallel 4 --group mystudy" in script

    def test_writes_setup_and_trial_to_temp_files(self):
        adapter = _make_adapter()
        params = _make_params(
            setup_command="wrapped_setup",
            trial_command="wrapped_trial",
        )
        script = adapter.render_sweep(params)

        assert "cat > /tmp/jernerics_mystudy_setup.sh" in script
        assert "wrapped_setup" in script
        assert "cat > /tmp/jernerics_mystudy_trial.sh" in script
        assert "wrapped_trial" in script

    def test_submits_setup_then_trials_with_after(self):
        adapter = _make_adapter()
        params = _make_params(n_trials=3)
        script = adapter.render_sweep(params)

        assert "pueue add -g mystudy --label mystudy_setup" in script
        assert (
            "pueue add -g mystudy --after $SETUP_ID --label mystudy_trial_1" in script
        )
        assert (
            "pueue add -g mystudy --after $SETUP_ID --label mystudy_trial_2" in script
        )
        assert (
            "pueue add -g mystudy --after $SETUP_ID --label mystudy_trial_3" in script
        )

    def test_max_parallel_overrides_config(self):
        adapter = _make_adapter(parallel=2)
        params = _make_params(n_trials=5, max_parallel=8)
        script = adapter.render_sweep(params)

        assert "pueue parallel 8 --group mystudy" in script

    def test_max_parallel_none_uses_config(self):
        adapter = _make_adapter(parallel=3)
        params = _make_params(n_trials=5, max_parallel=None)
        script = adapter.render_sweep(params)

        assert "pueue parallel 3 --group mystudy" in script

    def test_creates_optuna_and_tracking_dirs(self):
        adapter = _make_adapter(cache_dir="$HOME/.cache/jernerics")
        params = _make_params()
        script = adapter.render_sweep(params)

        cache = "$HOME/.cache/jernerics"
        assert f"mkdir -p {cache}/optuna {cache}/tracking/mystudy" in script

    def test_no_checker_without_post_hook(self):
        adapter = _make_adapter()
        params = _make_params()
        script = adapter.render_sweep(params)

        assert "pueue wait" not in script
        assert "checker" not in script

    def test_post_hook_creates_checker_with_wait(self):
        adapter = _make_adapter()
        params = _make_params(
            n_trials=3,
            post_hook_command="wrapped_checker",
        )
        script = adapter.render_sweep(params)

        assert "pueue wait $TRIAL_1_ID $TRIAL_2_ID $TRIAL_3_ID -q" in script
        assert "wrapped_checker" in script
        assert "pueue add -g mystudy --label mystudy_checker" in script

    def test_post_hook_writes_checker_to_temp_files(self):
        adapter = _make_adapter()
        params = _make_params(post_hook_command="wrapped_checker")
        script = adapter.render_sweep(params)

        assert "/tmp/jernerics_mystudy_checker.sh" in script
        assert "/tmp/jernerics_mystudy_wait_and_check.sh" in script


class TestSubmitSweep:
    def test_submits_and_returns_group_id(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="")
        adapter = _make_adapter(host=host)
        params = _make_params(n_trials=5)

        result = adapter.submit_sweep(params)

        assert len(result.submissions) == 1
        assert result.submissions[0].job_id == "mystudy"
        assert result.submissions[0].n_trials == 5

    def test_raises_on_failure(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1, stderr="pueue error")
        adapter = _make_adapter(host=host)

        with pytest.raises(RuntimeError, match="Failed to submit sweep"):
            adapter.submit_sweep(_make_params())


class TestSubmitJob:
    def test_submits_single_job(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="42\n")
        adapter = _make_adapter(host=host)

        job_id = adapter.submit_job("echo hello", name="build")

        assert job_id == "42"


class TestJobLifecycle:
    def test_list_jobs(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "tasks": {
                        "1": {
                            "status": {"Running": {}},
                            "label": "myjob",
                        }
                    }
                }
            ),
        )
        adapter = _make_adapter(host=host)

        jobs = adapter.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == "1"
        assert jobs[0].status == "RUNNING"

    def test_list_jobs_filters_completed(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "tasks": {
                        "1": {"status": {"Running": {}}, "label": "a"},
                        "2": {"status": {"Done": {"result": "Success"}}, "label": "b"},
                    }
                }
            ),
        )
        adapter = _make_adapter(host=host)

        jobs = adapter.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == "1"

    def test_list_jobs_includes_completed(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "tasks": {
                        "1": {"status": {"Running": {}}, "label": "a"},
                        "2": {"status": {"Done": {"result": "Success"}}, "label": "b"},
                    }
                }
            ),
        )
        adapter = _make_adapter(host=host)

        jobs = adapter.list_jobs(include_completed=True)
        assert len(jobs) == 2

    def test_cancel_task(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        assert adapter.cancel("42") is True
        host.run.assert_called_with(
            ["pueue", "kill", "42"], check=False, capture_output=True
        )

    def test_cancel_group(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        assert adapter.cancel("mystudy") is True
        host.run.assert_called_with(
            ["pueue", "kill", "--group", "mystudy"],
            check=False,
            capture_output=True,
        )

    def test_cancel_all(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        assert adapter.cancel_all() is True

    def test_get_status(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"tasks": {"5": {"status": {"Running": {}}, "label": "x"}}}
            ),
        )
        adapter = _make_adapter(host=host)

        assert adapter.get_status("5") == "RUNNING"

    def test_get_status_missing(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"tasks": {}})
        )
        adapter = _make_adapter(host=host)

        assert adapter.get_status("999") is None

    def test_wait_for_completion_task(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"tasks": {"5": {"status": {"Running": {}}, "label": "x"}}}
                ),
            ),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "tasks": {
                            "5": {
                                "status": {"Done": {"result": "Success"}},
                                "label": "x",
                            }
                        }
                    }
                ),
            ),
        ]
        adapter = _make_adapter(host=host)

        result = adapter.wait_for_completion("5", poll_interval=0.01)
        assert result is True

    def test_wait_for_completion_group(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "tasks": {
                            "1": {
                                "status": {"Running": {}},
                                "label": "x",
                                "group": "mystudy",
                            }
                        }
                    }
                ),
            ),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "tasks": {
                            "1": {
                                "status": {"Done": {"result": "Success"}},
                                "label": "x",
                                "group": "mystudy",
                            }
                        }
                    }
                ),
            ),
        ]
        adapter = _make_adapter(host=host)

        result = adapter.wait_for_completion("mystudy", poll_interval=0.01)
        assert result is True


class TestGetLogs:
    def test_get_logs_prints_output(self, capsys):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"5": {"output": "trial output"}}),
        )
        adapter = _make_adapter(host=host)

        adapter.get_logs("5")

        assert "trial output" in capsys.readouterr().out

    def test_get_logs_rejects_non_numeric(self):
        adapter = _make_adapter()

        with pytest.raises(SystemExit):
            adapter.get_logs("mystudy")

    def test_get_logs_follow(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        adapter.get_logs("5", follow=True)

        host.run.assert_called_with(["pueue", "follow", "5"], check=False)


class TestFromConfig:
    def test_constructs_from_config(self):
        from jernerics.backend.host import LocalHost

        config = BackendConfig(
            shared=SharedConfig(
                name="local-pueue",
                type="pueue",
                host=None,
                remote_dir="~/projects/proj",
                cache_dir="~/.cache/jernerics",
                container_type="docker",
            ),
            backend=PueueConfig(parallel=4),
        )
        adapter = PueueAdapter.from_config(config, host=LocalHost())

        assert isinstance(adapter, SchedulerAdapter)
        assert adapter.parallel == 4
        assert "~" not in adapter.remote_dir


class TestCleanup:
    def test_cleanup_runs_pueue_clean(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        adapter.cleanup()

        host.run.assert_called_with(
            ["pueue", "clean"], check=False, capture_output=True
        )


class TestDaemonError:
    def test_raises_on_daemon_unreachable(self):
        from jernerics.backend.pueue.adapter import PueueDaemonError

        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1, stderr="connection refused")
        adapter = _make_adapter(host=host)

        with pytest.raises(PueueDaemonError):
            adapter.list_jobs()
