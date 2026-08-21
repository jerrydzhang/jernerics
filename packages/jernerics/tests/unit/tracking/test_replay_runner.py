from unittest.mock import patch

import pytest
from jernerics.tracking.batch_sync import ReplayResult
from jernerics.tracking.replay_runner import main


class TestMainServerAddr:
    def test_scheme_less_server_exits_immediately_without_replay(self, capsys):
        with (
            patch("jernerics.tracking.replay_runner.replay_tracking") as mock_replay,
            pytest.raises(SystemExit) as excinfo,
        ):
            main(
                [
                    "--tracking-dir",
                    "/tmp/tracking",
                    "--server-addr",
                    "atlas.taile454b.ts.net:443",
                ]
            )

        assert excinfo.value.code == 1
        mock_replay.assert_not_called()
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "JERNERICS_TRACKING_SERVER" in err
        assert "[tool.jernerics] tracking_server" in err
        assert "atlas.taile454b.ts.net:443" in err

    def test_valid_server_invokes_replay(self):
        with patch(
            "jernerics.tracking.replay_runner.replay_tracking",
            return_value=ReplayResult(),
        ) as mock_replay:
            main(
                [
                    "--tracking-dir",
                    "/tmp/tracking",
                    "--server-addr",
                    "http://localhost:8000",
                ]
            )

        mock_replay.assert_called_once()
        assert mock_replay.call_args.kwargs["base_url"] == "http://localhost:8000"
