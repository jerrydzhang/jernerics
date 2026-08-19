import json
import shutil
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from jernerics.sync.mutagen_sync import SessionInfo
from jernerics.sync.resolve import (
    EndpointError,
    LocalEndpoint,
    PathPlan,
    ResolveError,
    SourceSide,
    backups_root,
    new_run_id,
    normalize_rel,
    resolve_conflicts,
    sha256_file,
)

SESSION = "jernerics-interactive-proj"


class FakeSync:
    """Duck-typed MutagenSync: canned conflicts, flush swaps in post state."""

    def __init__(self, record, conflicts):
        self.record = record
        self.conflicts = list(conflicts)
        self.after_flush: list[str] = []
        self.flushed: list[str] = []

    def list_sessions(self):
        return [self.record]

    def conflicted_paths(self, name):
        return list(self.conflicts)

    def flush(self, name):
        self.flushed.append(name)
        self.conflicts = list(self.after_flush)

    def wait_idle(self, name, timeout=60):
        return True


class Env:
    def __init__(self, tmp_path: Path):
        self.alpha = tmp_path / "alpha"
        self.beta = tmp_path / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.record = SessionInfo(
            name=SESSION,
            status="Conflict resolution required",
            alpha_path=str(self.alpha),
            beta_path=str(self.beta),
            alpha_connected=True,
            beta_connected=True,
            conflicts=1,
        )
        self.backups = tmp_path / "state-backups"

    def conflict(self, rel: str, alpha_text: str, beta_text: str) -> None:
        target = self.alpha / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(alpha_text)
        beta_target = self.beta / rel
        beta_target.parent.mkdir(parents=True, exist_ok=True)
        beta_target.write_text(beta_text)

    def sync(self, conflicts) -> FakeSync:
        return FakeSync(self.record, conflicts)

    def resolve(self, sync, paths, source=SourceSide.LOCAL, **kwargs):
        kwargs.setdefault("assume_yes", True)
        return resolve_conflicts(
            sync,
            SESSION,
            paths,
            source,
            "proj",
            base_dir=self.backups,
            **kwargs,
        )


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


class TestNormalizeRel:
    def test_collapses_redundant_separators(self):
        assert normalize_rel("./src//a.py") == "src/a.py"

    def test_refuses_absolute(self):
        with pytest.raises(ResolveError, match="absolute"):
            normalize_rel("/etc/passwd")

    def test_refuses_parent_traversal(self):
        with pytest.raises(ResolveError, match="traversal"):
            normalize_rel("src/../secrets")

    def test_refuses_empty_and_dot(self):
        with pytest.raises(ResolveError):
            normalize_rel("")
        with pytest.raises(ResolveError):
            normalize_rel(".")


class TestBackupPaths:
    def test_backups_root_honors_xdg_state_home(self, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
        assert backups_root() == Path("/custom/state/jernerics/sync-backups")

    def test_backups_root_defaults_to_local_state(self, monkeypatch):
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert backups_root() == Path.home() / ".local/state/jernerics/sync-backups"

    def test_run_ids_are_unique_and_utc_stamped(self):
        first = new_run_id()
        second = new_run_id()
        assert first != second
        assert first[:16].endswith("Z")
        assert "T" in first


class TestLocalWinner:
    def test_cluster_file_replaced_and_loser_backed_up(self, env):
        env.conflict("src/a.py", "local wins", "cluster loses")
        report = env.resolve(env.sync(["src/a.py"]), ["src/a.py"], SourceSide.LOCAL)

        assert report.ok
        assert (env.beta / "src/a.py").read_text() == "local wins"
        assert (env.alpha / "src/a.py").read_text() == "local wins"
        assert report.run_dir is not None
        backup = report.run_dir / "files" / "src/a.py"
        assert backup.read_text() == "cluster loses"
        assert report.outcomes["src/a.py"].backup_sha == sha256_file(backup)
        assert report.outcomes["src/a.py"].conflict_cleared is True


class TestClusterWinner:
    def test_local_file_replaced_and_loser_backed_up(self, env):
        env.conflict("src/a.py", "local loses", "cluster wins")
        report = env.resolve(env.sync(["src/a.py"]), ["src/a.py"], SourceSide.CLUSTER)

        assert report.ok
        assert (env.alpha / "src/a.py").read_text() == "cluster wins"
        backup = report.run_dir / "files" / "src/a.py"
        assert backup.read_text() == "local loses"
        assert report.plans[0].direction == "cluster -> local"
        assert report.plans[0].loser_side == "local"


class TestMultiplePaths:
    def test_all_selected_paths_resolved(self, env):
        env.conflict("a.txt", "a-local", "a-cluster")
        env.conflict("src/b.py", "b-local", "b-cluster")
        env.conflict("src/deep/c.py", "c-local", "c-cluster")
        sync = env.sync(["a.txt", "src/b.py", "src/deep/c.py"])
        report = env.resolve(
            sync, ["a.txt", "src/deep/c.py", "src/b.py"], SourceSide.LOCAL
        )

        assert report.ok
        assert report.completed == ["a.txt", "src/deep/c.py", "src/b.py"]
        for rel in report.completed:
            assert (env.beta / rel).read_text() == f"{Path(rel).stem}-local"

    def test_duplicate_paths_are_deduped(self, env):
        env.conflict("a.txt", "local", "cluster")
        report = env.resolve(
            env.sync(["a.txt"]), ["a.txt", "./a.txt"], SourceSide.LOCAL
        )
        assert report.completed == ["a.txt"]
        assert len(report.plans) == 1


class TestMixedRemainingConflicts:
    def test_unselected_conflicts_do_not_fail_the_invocation(self, env):
        env.conflict("a.txt", "local", "cluster")
        env.conflict("b.txt", "local", "cluster")
        sync = env.sync(["a.txt", "b.txt"])
        sync.after_flush = ["b.txt"]
        report = env.resolve(sync, ["a.txt"], SourceSide.LOCAL)

        assert report.ok
        assert report.outcomes["a.txt"].conflict_cleared is True


class TestDryRun:
    def test_preview_only_no_backup_no_mutation(self, env):
        env.conflict("a.txt", "local", "cluster")
        seen: list[tuple[list[PathPlan], Path]] = []

        def on_plans(plans, run_dir):
            seen.append((plans, run_dir))

        report = env.resolve(
            env.sync(["a.txt"]),
            ["a.txt"],
            SourceSide.LOCAL,
            dry_run=True,
            on_plans=on_plans,
        )

        assert report.ok
        assert report.dry_run
        assert len(seen) == 1
        plans, run_dir = seen[0]
        assert plans[0].rel == "a.txt"
        assert plans[0].backup_path == run_dir / "files" / "a.txt"
        assert (env.beta / "a.txt").read_text() == "cluster"
        assert (env.alpha / "a.txt").read_text() == "local"
        assert not env.backups.exists()
        assert not run_dir.exists()


class TestRefusals:
    def test_path_not_in_conflict_list(self, env):
        env.conflict("a.txt", "local", "cluster")
        with pytest.raises(ResolveError, match="not in the current conflict list"):
            env.resolve(env.sync(["a.txt"]), ["other.txt"], SourceSide.LOCAL)
        assert not env.backups.exists()

    def test_missing_on_an_endpoint(self, env):
        env.conflict("a.txt", "local", "cluster")
        (env.beta / "a.txt").unlink()
        with pytest.raises(ResolveError, match="missing on the cluster side"):
            env.resolve(env.sync(["a.txt"]), ["a.txt"], SourceSide.LOCAL)

    def test_directory_on_an_endpoint(self, env):
        env.conflict("a.txt", "local", "cluster")
        (env.beta / "a.txt").unlink()
        (env.beta / "a.txt").mkdir()
        with pytest.raises(ResolveError, match="a directory on the cluster side"):
            env.resolve(env.sync(["a.txt"]), ["a.txt"], SourceSide.LOCAL)

    def test_symlink_on_an_endpoint(self, env):
        env.conflict("a.txt", "local", "cluster")
        (env.beta / "target.txt").write_text("elsewhere")
        (env.beta / "a.txt").unlink()
        (env.beta / "a.txt").symlink_to("target.txt")
        with pytest.raises(ResolveError, match="a symlink on the cluster side"):
            env.resolve(env.sync(["a.txt"]), ["a.txt"], SourceSide.LOCAL)

    def test_parent_traversal_path(self, env):
        env.conflict("a.txt", "local", "cluster")
        with pytest.raises(ResolveError, match="traversal"):
            env.resolve(env.sync(["../a.txt"]), ["../a.txt"], SourceSide.LOCAL)

    def test_absolute_path(self, env):
        with pytest.raises(ResolveError, match="absolute"):
            env.resolve(env.sync([]), ["/etc/passwd"], SourceSide.LOCAL)

    def test_no_yes_in_noninteractive_mode(self, env):
        env.conflict("a.txt", "local", "cluster")
        with pytest.raises(ResolveError, match=r"noninteractive.*--yes"):
            env.resolve(
                env.sync(["a.txt"]),
                ["a.txt"],
                SourceSide.LOCAL,
                assume_yes=False,
                is_interactive=False,
            )
        assert (env.beta / "a.txt").read_text() == "cluster"

    def test_declined_confirmation_changes_nothing(self, env):
        env.conflict("a.txt", "local", "cluster")
        with pytest.raises(ResolveError, match="declined"):
            env.resolve(
                env.sync(["a.txt"]),
                ["a.txt"],
                SourceSide.LOCAL,
                assume_yes=False,
                is_interactive=True,
                confirm=lambda msg: False,
            )
        assert (env.beta / "a.txt").read_text() == "cluster"
        assert not env.backups.exists()

    def test_empty_path_list_refused(self, env):
        with pytest.raises(ResolveError, match="at least one PATH"):
            env.resolve(env.sync([]), [], SourceSide.LOCAL)

    def test_missing_session_refused(self, env):
        sync = env.sync(["a.txt"])
        sync.record = SessionInfo(
            name="other",
            status="Watching",
            alpha_path=str(env.alpha),
            beta_path=str(env.beta),
            alpha_connected=True,
            beta_connected=True,
            conflicts=0,
        )
        with pytest.raises(ResolveError, match="not found"):
            env.resolve(sync, ["a.txt"], SourceSide.LOCAL)

    def test_disconnected_endpoint_refused(self, env):
        env.conflict("a.txt", "local", "cluster")
        sync = env.sync(["a.txt"])
        sync.record = SessionInfo(
            name=SESSION,
            status="Connecting",
            alpha_path=str(env.alpha),
            beta_path=str(env.beta),
            alpha_connected=True,
            beta_connected=False,
            conflicts=1,
        )
        with pytest.raises(ResolveError, match="disconnected endpoint"):
            env.resolve(sync, ["a.txt"], SourceSide.LOCAL)

    def test_insufficient_destination_space(self, env, monkeypatch):
        env.conflict("a.txt", "local", "cluster")

        def tiny_free(self):
            return 1

        monkeypatch.setattr(LocalEndpoint, "free_bytes", tiny_free)
        with pytest.raises(ResolveError, match="destination has 1 bytes free"):
            env.resolve(env.sync(["a.txt"]), ["a.txt"], SourceSide.LOCAL)
        assert (env.beta / "a.txt").read_text() == "cluster"

    def test_insufficient_local_backup_space(self, env, monkeypatch):
        env.conflict("a.txt", "local", "cluster")
        du = namedtuple("du", "total used free")
        monkeypatch.setattr(
            "jernerics.sync.resolve.shutil.disk_usage",
            lambda path: du(0, 0, 1) if path != env.beta else du(0, 0, 10**9),
        )
        with pytest.raises(ResolveError, match="local backup storage"):
            env.resolve(env.sync(["a.txt"]), ["a.txt"], SourceSide.LOCAL)
        assert (env.beta / "a.txt").read_text() == "cluster"

    def test_source_changed_after_preflight_aborts_before_mutation(self, env):
        env.conflict("a.txt", "local", "cluster")

        def mutate_winner(plans, run_dir):
            (env.alpha / "a.txt").write_text("changed mid-flight")

        report = env.resolve(
            env.sync(["a.txt"]),
            ["a.txt"],
            SourceSide.LOCAL,
            on_plans=mutate_winner,
        )

        assert not report.ok
        assert "race detected" in report.error
        assert report.unresolved == ["a.txt"]
        assert (env.beta / "a.txt").read_text() == "cluster"
        assert (env.alpha / "a.txt").read_text() == "changed mid-flight"

    def test_loser_edited_after_preflight_aborts_at_backup_verification(self, env):
        env.conflict("a.txt", "local", "cluster")

        def mutate_loser(plans, run_dir):
            (env.beta / "a.txt").write_text("loser edited concurrently")

        report = env.resolve(
            env.sync(["a.txt"]),
            ["a.txt"],
            SourceSide.LOCAL,
            on_plans=mutate_loser,
        )

        assert not report.ok
        assert "does not match its preflight checksum" in report.error
        assert (env.beta / "a.txt").read_text() == "loser edited concurrently"

    def test_destination_race_between_paths_detected_at_rehash(self, env, monkeypatch):
        env.conflict("a.txt", "a local", "a cluster")
        env.conflict("b.txt", "b local", "b cluster")
        real_install = LocalEndpoint.install

        def touch_next_loser(self, rel, src, expected_sha):
            result = real_install(self, rel, src, expected_sha)
            if rel == "a.txt":
                (env.beta / "b.txt").write_text("loser edited mid-run")
            return result

        monkeypatch.setattr(LocalEndpoint, "install", touch_next_loser)
        report = env.resolve(env.sync(["a.txt", "b.txt"]), ["a.txt", "b.txt"])

        assert not report.ok
        assert "race detected" in report.error
        assert report.completed == ["a.txt"]
        assert report.unresolved == ["b.txt"]
        assert (env.beta / "b.txt").read_text() == "loser edited mid-run"


class TestBackupFailure:
    def test_backup_failure_mutates_nothing(self, env, monkeypatch):
        env.conflict("a.txt", "local", "cluster")
        env.conflict("b.txt", "local", "cluster")

        def failing_fetch(self, rel, dest):
            raise EndpointError("simulated quota exhaustion")

        monkeypatch.setattr(LocalEndpoint, "fetch_to", failing_fetch)
        report = env.resolve(env.sync(["a.txt", "b.txt"]), ["a.txt", "b.txt"])

        assert not report.ok
        assert "backup of losers failed" in report.error
        assert (env.beta / "a.txt").read_text() == "cluster"
        assert (env.beta / "b.txt").read_text() == "cluster"
        assert report.unresolved == ["a.txt"]

    def test_backup_checksum_mismatch_mutates_nothing(self, env, monkeypatch):
        env.conflict("a.txt", "local", "cluster")
        real_fetch = LocalEndpoint.fetch_to

        def tamper(self, rel, dest):
            real_fetch(self, rel, dest)
            if "files" in dest.parts:
                dest.write_text("tampered backup")

        monkeypatch.setattr(LocalEndpoint, "fetch_to", tamper)
        report = env.resolve(env.sync(["a.txt"]), ["a.txt"])

        assert not report.ok
        assert "does not match its preflight checksum" in report.error
        assert (env.beta / "a.txt").read_text() == "cluster"


class TestTransferFailure:
    def test_corrupt_temp_stops_and_cleans(self, env, monkeypatch):
        env.conflict("a.txt", "local", "cluster")
        real_copy = shutil.copyfile

        def corrupt_tmp(src, dst, **kwargs):
            if str(dst).endswith(".tmp"):
                Path(dst).write_bytes(b"corrupted in transit")
                return None
            return real_copy(src, dst, **kwargs)

        monkeypatch.setattr("jernerics.sync.resolve.shutil.copyfile", corrupt_tmp)
        report = env.resolve(env.sync(["a.txt"]), ["a.txt"])

        assert not report.ok
        assert "failed checksum verification" in report.error
        assert report.unresolved == ["a.txt"]
        assert (env.beta / "a.txt").read_text() == "cluster"
        assert list((env.beta).glob("*.tmp")) == []


class TestPartialCompletion:
    def test_exact_completed_untouched_unresolved_report(self, env, monkeypatch):
        for name in ("a.txt", "b.txt", "c.txt"):
            env.conflict(name, f"{name} local", f"{name} cluster")
        real_install = LocalEndpoint.install

        def fail_middle(self, rel, src, expected_sha):
            if rel == "b.txt":
                raise EndpointError("simulated transfer failure")
            return real_install(self, rel, src, expected_sha)

        monkeypatch.setattr(LocalEndpoint, "install", fail_middle)
        report = env.resolve(
            env.sync(["a.txt", "b.txt", "c.txt"]),
            ["a.txt", "b.txt", "c.txt"],
        )

        assert not report.ok
        assert report.completed == ["a.txt"]
        assert report.unresolved == ["b.txt"]
        assert report.untouched == ["c.txt"]
        assert (env.beta / "a.txt").read_text() == "a.txt local"
        assert (env.beta / "b.txt").read_text() == "b.txt cluster"
        assert (env.beta / "c.txt").read_text() == "c.txt cluster"
        assert report.run_dir is not None
        assert (report.run_dir / "manifest.json").is_file()


class TestConflictClearVerification:
    def test_selected_path_still_conflicted_reports_failure(self, env):
        env.conflict("a.txt", "local", "cluster")
        sync = env.sync(["a.txt"])
        sync.after_flush = ["a.txt"]
        report = env.resolve(sync, ["a.txt"])

        assert not report.ok
        assert "still conflicted" in report.error
        assert report.unresolved == ["a.txt"]
        assert report.outcomes["a.txt"].conflict_cleared is False
        assert (env.beta / "a.txt").read_text() == "local"

    def test_flush_failure_reports_without_raising(self, env):
        env.conflict("a.txt", "local", "cluster")
        sync = env.sync(["a.txt"])

        def bad_flush(name):
            from jernerics.sync.mutagen_sync import MutagenError

            raise MutagenError("daemon exploded")

        sync.flush = bad_flush
        report = env.resolve(sync, ["a.txt"])

        assert not report.ok
        assert "conflict re-check failed" in report.error
        assert (env.beta / "a.txt").read_text() == "local"


class TestManifest:
    def test_manifest_records_full_protocol_state(self, env):
        env.conflict("src/a.py", "local wins", "cluster loses")
        report = env.resolve(env.sync(["src/a.py"]), ["src/a.py"], SourceSide.LOCAL)

        assert report.run_dir is not None
        manifest = json.loads((report.run_dir / "manifest.json").read_text())
        assert manifest["session"] == SESSION
        assert manifest["source"] == "local"
        assert manifest["endpoints"]["local"] == str(env.alpha)
        assert manifest["endpoints"]["cluster"] == str(env.beta)
        assert manifest["error"] is None
        assert manifest["started_utc"]
        assert manifest["finished_utc"]
        entry = manifest["paths"][0]
        assert entry["path"] == "src/a.py"
        assert entry["status"] == "completed"
        assert entry["mutation"] == "succeeded"
        assert entry["conflict_cleared"] is True
        assert entry["original"]["winner"]["sha256"] == sha256_file(
            env.alpha / "src/a.py"
        )
        assert entry["original"]["winner"]["side"] == "local"
        assert entry["original"]["loser"]["sha256"] == sha256_file(
            report.run_dir / "files" / "src/a.py"
        )
        assert entry["original"]["loser"]["side"] == "cluster"
        assert entry["backup"]["sha256"] == entry["original"]["loser"]["sha256"]

    def test_manifest_written_even_after_failure(self, env, monkeypatch):
        env.conflict("a.txt", "local", "cluster")

        def failing_fetch(self, rel, dest):
            raise EndpointError("no space left")

        monkeypatch.setattr(LocalEndpoint, "fetch_to", failing_fetch)
        report = env.resolve(env.sync(["a.txt"]), ["a.txt"])

        manifest = json.loads((report.run_dir / "manifest.json").read_text())
        assert manifest["error"] is not None
        assert manifest["paths"][0]["status"] == "unresolved"
        assert manifest["paths"][0]["mutation"] == "not attempted"


class TestCliWiring:
    def test_resolve_command_passes_args_to_core(self, capsys, tmp_path):
        from jernerics.commands import interactive

        stub = SimpleNamespace(
            dry_run=False,
            ok=True,
            error=None,
            completed=["a.txt"],
            unresolved=[],
            untouched=[],
            run_dir=tmp_path / "run",
            outcomes={"a.txt": SimpleNamespace(error=None)},
        )
        with (
            patch(
                "jernerics.commands.interactive.find_pyproject_dir",
                return_value=tmp_path,
            ),
            patch("jernerics.commands.interactive.load_backend_config"),
            patch(
                "jernerics.commands.interactive.get_project_name",
                return_value="proj",
            ),
            patch("jernerics.commands.interactive.MutagenSync") as ms,
            patch(
                "jernerics.commands.interactive.resolve_conflicts",
                return_value=stub,
            ) as core,
        ):
            ms.available.return_value = True
            interactive.sync_resolve(
                paths=[Path("a.txt")],
                backend_name="hpc",
                source=SourceSide.LOCAL,
                dry_run=False,
                yes=True,
            )

        assert core.call_args.args[1] == "jernerics-interactive-proj"
        assert core.call_args.args[2] == ["a.txt"]
        assert core.call_args.args[3] is SourceSide.LOCAL
        assert core.call_args.kwargs["assume_yes"] is True
        out = capsys.readouterr().out
        assert "Resolved 1 path(s)" in out
