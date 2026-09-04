import json
from types import SimpleNamespace

from jernerics.backend.pueue.adapter import PueueAdapter
from jernerics.config import BackendConfig, PueueConfig, SharedConfig

STUDY_A = "sweep-e2e_config_20260901-000000"
STUDY_B = "sweep-e2e_gpu_20260902-000000"
UNRELATED_GROUPS = ("colleagues-sweep", "default")


class FakeHost:
    home = "/home/u"
    is_local = True

    def __init__(self, tracked_studies=(), daemon_tasks=None, tracking_missing=False):
        self.commands = []
        self.tracked_studies = list(tracked_studies)
        self.daemon_tasks = daemon_tasks or {}
        self.tracking_missing = tracking_missing

    def run(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:2] == ["ls", "-1"]:
            if self.tracking_missing:
                return SimpleNamespace(returncode=1, stdout="", stderr="no dir")
            return SimpleNamespace(
                returncode=0, stdout="\n".join(self.tracked_studies), stderr=""
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"tasks": self.daemon_tasks}), stderr=""
        )

    def shell(self, command, **kwargs):
        return self.run(["sh", "-c", command], **kwargs)


def _daemon_task(group, label):
    return {"group": group, "label": label, "status": {"Done": {}}}


def _make_host():
    return FakeHost(
        tracked_studies=[STUDY_A, STUDY_B],
        daemon_tasks={
            "1": _daemon_task(STUDY_A, f"{STUDY_A}_setup"),
            "2": _daemon_task(STUDY_B, f"{STUDY_B}_trial_1"),
            "3": _daemon_task("colleagues-sweep", "colleagues_task"),
            "4": _daemon_task("default", "build"),
        },
    )


def _make_adapter(host):
    return PueueAdapter(
        host=host,
        remote_dir="/home/u/experiments/sweep-e2e",
        cache_dir="/home/u/.cache/jernerics/sweep-e2e",
        parallel=2,
    )


def _clean_commands(host):
    return [c for c in host.commands if c[:2] == ["pueue", "clean"]]


class TestCleanupScoping:
    def test_cleans_each_tracked_group_once(self):
        host = _make_host()
        adapter = _make_adapter(host)

        adapter.cleanup()

        assert _clean_commands(host) == [
            ["pueue", "clean", "--group", STUDY_A],
            ["pueue", "clean", "--group", f"{STUDY_A}_checker"],
            ["pueue", "clean", "--group", STUDY_B],
            ["pueue", "clean", "--group", f"{STUDY_B}_checker"],
        ]

    def test_unrelated_group_tasks_survive(self):
        host = _make_host()
        adapter = _make_adapter(host)

        adapter.cleanup()

        cleans = _clean_commands(host)
        assert cleans
        assert all(cmd[2] == "--group" for cmd in cleans)
        assert all(cmd[3] not in UNRELATED_GROUPS for cmd in cleans)
        assert not any("colleagues-sweep" in cmd for cmd in host.commands)

    def test_no_global_clean_anywhere(self):
        host = _make_host()
        adapter = _make_adapter(host)

        adapter.cleanup()

        assert ["pueue", "clean"] not in host.commands

    def test_missing_tracking_dir_cleans_nothing(self):
        host = FakeHost(tracking_missing=True)
        adapter = _make_adapter(host)

        adapter.cleanup()

        assert _clean_commands(host) == []

    def test_empty_tracking_dir_cleans_nothing(self):
        host = FakeHost(tracked_studies=[])
        adapter = _make_adapter(host)

        adapter.cleanup()

        assert _clean_commands(host) == []


class TestFromConfigCacheScope:
    def test_appends_project_to_cache_dir(self):
        config = BackendConfig(
            shared=SharedConfig(
                name="pueue-remote", type="pueue", cache_dir="~/.cache/jernerics"
            ),
            backend=PueueConfig(),
        )

        adapter = PueueAdapter.from_config(
            config, host=FakeHost(), project_name="sweep-e2e"
        )

        assert adapter.cache_dir == "/home/u/.cache/jernerics/sweep-e2e"

    def test_substitutes_project_template_in_cache_dir(self):
        config = BackendConfig(
            shared=SharedConfig(
                name="pueue-remote", type="pueue", cache_dir="~/cache/{project_name}"
            ),
            backend=PueueConfig(),
        )

        adapter = PueueAdapter.from_config(
            config, host=FakeHost(), project_name="sweep-e2e"
        )

        assert adapter.cache_dir == "/home/u/cache/sweep-e2e"
