from pathlib import Path
from unittest.mock import MagicMock

from jernerics.tracking.artifact_manifest import ArtifactManifest
from jernerics.tracking.artifact_uploader import ArtifactUploader


class TestArtifactUploader:
    def test_uploads_entries_from_manifest(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"
        manifest = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        manifest.append("model.pt", "/work/model.pt")
        manifest.append("plot.png", "/work/plot.png")

        mock_uploader = MagicMock()
        uploader = ArtifactUploader(
            manifest_path=manifest_path,
            cursor_path=cursor_path,
            upload_fn=mock_uploader,
            project="myproj",
            study="mystudy",
            trial_id=0,
            poll_interval=0.05,
        )

        uploader.start()
        uploader.join(timeout=5)

        assert mock_uploader.call_count == 2
        calls = mock_uploader.call_args_list
        assert calls[0][0][0] == "myproj/mystudy/0/model.pt"
        assert calls[0][0][1] == "/work/model.pt"
        assert calls[1][0][0] == "myproj/mystudy/0/plot.png"

    def test_cursor_advances_after_upload(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"
        manifest = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        manifest.append("a.pt", "/work/a.pt")
        manifest.append("b.pt", "/work/b.pt")

        mock_uploader = MagicMock()
        uploader = ArtifactUploader(
            manifest_path=manifest_path,
            cursor_path=cursor_path,
            upload_fn=mock_uploader,
            project="p",
            study="s",
            trial_id=1,
            poll_interval=0.05,
        )

        uploader.start()
        uploader.join(timeout=5)

        remaining = manifest.read_from_cursor()
        assert remaining == []

    def test_no_entries_no_uploads(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.touch()

        mock_uploader = MagicMock()
        uploader = ArtifactUploader(
            manifest_path=manifest_path,
            cursor_path=cursor_path,
            upload_fn=mock_uploader,
            project="p",
            study="s",
            trial_id=0,
            poll_interval=0.05,
        )

        uploader.start()
        uploader.join(timeout=5)

        assert mock_uploader.call_count == 0

    def test_s3_key_format(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"
        manifest = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        manifest.append("ckpt", "/work/ckpt")

        mock_uploader = MagicMock()
        uploader = ArtifactUploader(
            manifest_path=manifest_path,
            cursor_path=cursor_path,
            upload_fn=mock_uploader,
            project="my-proj",
            study="my-study",
            trial_id=42,
            poll_interval=0.05,
        )

        uploader.start()
        uploader.join(timeout=5)

        s3_key = mock_uploader.call_args[0][0]
        assert s3_key == "my-proj/my-study/42/ckpt"
