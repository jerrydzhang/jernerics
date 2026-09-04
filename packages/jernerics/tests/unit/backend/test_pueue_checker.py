import json
from unittest.mock import MagicMock

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.pueue.adapter import PueueAdapter, pueue_group_from_label


def _make_adapter(host=None, **overrides):
    defaults = {
        "remote_dir": "$HOME/projects/proj",
        "cache_dir": "$HOME/.cache/jernerics",
        "parallel": 4,
    }
    defaults.update(overrides)
    if host is None:
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="0")
    return PueueAdapter(host=host, **defaults)


def _make_params(**overrides):
    defaults = {
        "setup_command": "wrapped_setup",
        "trial_command": "wrapped_trial",
        "n_trials": 3,
        "study_name": "mystudy",
        "log_dir": "/cache/logs",
        "post_hook_command": "wrapped_checker",
    }
    defaults.update(overrides)
    return SweepSubmissionParams(**defaults)


def _status(tasks):
    return MagicMock(returncode=0, stdout=json.dumps({"tasks": tasks}), stderr="")


def _task(group, status):
    return {"group": group, "status": status}


_DONE_OK = {
    "result": "Success",
    "start": "2026-09-04T10:00:00+00:00",
    "end": "2026-09-04T10:00:10+00:00",
}


class TestCheckerGroupPlacement:
    def test_checker_gets_dedicated_group(self):
        script = _make_adapter().render_sweep(_make_params())

        assert "pueue group add mystudy_checker 2>/dev/null || true" in script

    def test_checker_group_is_serial(self):
        script = _make_adapter().render_sweep(_make_params())

        assert "pueue parallel 1 --group mystudy_checker" in script

    def test_checker_task_lands_in_checker_group(self):
        script = _make_adapter().render_sweep(_make_params())

        assert (
            "pueue add -g mystudy_checker"
            " --label mystudy_checker"
            " -- bash /tmp/jernerics_mystudy_wait_and_check.sh"
        ) in script

    def test_sweep_group_keeps_all_slots_for_trials(self):
        script = _make_adapter(parallel=4).render_sweep(_make_params())

        assert "pueue parallel 4 --group mystudy" in script
        assert (
            "pueue add -g mystudy --after $SETUP_ID --label mystudy_trial_1" in script
        )
        assert "pueue add -g mystudy --label mystudy_checker" not in script

    def test_no_checker_group_without_post_hook(self):
        script = _make_adapter().render_sweep(_make_params(post_hook_command=None))

        assert "checker" not in script

    def test_wait_semantics_preserved(self):
        script = _make_adapter().render_sweep(_make_params(n_trials=3))

        assert "pueue wait $TRIAL_1_ID $TRIAL_2_ID $TRIAL_3_ID -q" in script
        assert "bash /tmp/jernerics_mystudy_checker.sh" in script

    def test_checker_label_still_resolves_to_sweep_group(self):
        assert pueue_group_from_label("mystudy_checker") == "mystudy"


class TestGroupViewCoversCheckerGroup:
    def test_resources_cover_checker_group(self):
        host = MagicMock()
        host.run.return_value = _status(
            {
                "1": _task("mystudy", {"Done": _DONE_OK}),
                "2": _task("mystudy_checker", {"Done": _DONE_OK}),
                "3": _task("other", {"Done": _DONE_OK}),
            }
        )
        adapter = _make_adapter(host=host)

        result = adapter.fetch_job_resources("mystudy")

        assert [snapshot.job_id for snapshot in result.snapshots] == ["1", "2"]

    def test_group_view_excludes_unrelated_groups(self):
        host = MagicMock()
        host.run.return_value = _status(
            {"1": _task("mystudy_checkerish", {"Done": _DONE_OK})}
        )
        adapter = _make_adapter(host=host)

        assert adapter.fetch_job_resources("mystudy").snapshots == []


class TestWaitForCompletionCoversCheckerGroup:
    def test_wait_blocks_until_checker_group_done(self):
        host = MagicMock()
        host.run.side_effect = [
            _status(
                {
                    "1": _task("mystudy", {"Done": {"result": "Success"}}),
                    "2": _task("mystudy_checker", {"Running": {}}),
                }
            ),
            _status(
                {
                    "1": _task("mystudy", {"Done": {"result": "Success"}}),
                    "2": _task("mystudy_checker", {"Done": {"result": "Success"}}),
                }
            ),
        ]
        adapter = _make_adapter(host=host)

        assert adapter.wait_for_completion("mystudy", poll_interval=0.01) is True
        assert host.run.call_count == 2

    def test_wait_reports_checker_failure(self):
        host = MagicMock()
        host.run.return_value = _status(
            {
                "1": _task("mystudy", {"Done": {"result": "Success"}}),
                "2": _task("mystudy_checker", {"Done": {"result": "Failed"}}),
            }
        )
        adapter = _make_adapter(host=host)

        assert adapter.wait_for_completion("mystudy", poll_interval=0.01) is False


class TestCancelAndCleanupCoverCheckerGroup:
    def test_cancel_group_kills_checker_group_too(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        assert adapter.cancel("mystudy") is True

        kills = [c.args[0] for c in host.run.call_args_list]
        assert kills == [
            ["pueue", "kill", "--group", "mystudy_checker"],
            ["pueue", "kill", "--group", "mystudy"],
        ]

    def test_cancel_group_tolerates_missing_checker_group(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]
        adapter = _make_adapter(host=host)

        assert adapter.cancel("mystudy") is True

    def test_cleanup_cleans_checker_group(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="mystudy\n", stderr="")
        adapter = _make_adapter(host=host)

        adapter.cleanup()

        cleans = [
            c.args[0]
            for c in host.run.call_args_list
            if c.args[0][:2] == ["pueue", "clean"]
        ]
        assert cleans == [
            ["pueue", "clean", "--group", "mystudy"],
            ["pueue", "clean", "--group", "mystudy_checker"],
        ]
