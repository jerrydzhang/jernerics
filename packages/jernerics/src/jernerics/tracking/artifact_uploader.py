from pathlib import Path
from threading import Event, Thread

from jernerics.tracking.artifact_manifest import ArtifactManifest


class ArtifactUploader:
    def __init__(
        self,
        manifest_path: Path,
        cursor_path: Path,
        upload_fn,
        project: str,
        study: str,
        trial_id: int,
        poll_interval: float = 0.5,
    ) -> None:
        self.manifest_path = manifest_path
        self.cursor_path = cursor_path
        self.upload_fn = upload_fn
        self.project = project
        self.study = study
        self.trial_id = trial_id
        self.poll_interval = poll_interval
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float = 60.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        manifest = ArtifactManifest(self.manifest_path, cursor_path=self.cursor_path)

        while True:
            entries = manifest.read_from_cursor()
            if entries:
                self._upload_entries(manifest, entries)
            elif self._stop.is_set():
                # Final drain after stop signal
                entries = manifest.read_from_cursor()
                if entries:
                    self._upload_entries(manifest, entries)
                return
            else:
                self._stop.wait(self.poll_interval)

    def _upload_entries(self, manifest: ArtifactManifest, entries: list[dict]) -> None:
        with open(self.manifest_path) as f:
            all_lines = f.read().split("\n")

        # Calculate byte offset for each line
        offset = manifest._read_cursor()
        byte_pos = offset
        lines_from_offset = all_lines
        # Recompute byte positions from beginning to handle cursor correctly
        content = "\n".join(all_lines)
        byte_pos = 0
        line_starts = []
        for line in all_lines:
            line_starts.append(byte_pos)
            byte_pos += len(line) + 1  # +1 for newline

        current_offset = manifest._read_cursor()
        lines_consumed = 0
        for _i, (start, line) in enumerate(zip(line_starts, all_lines, strict=True)):
            if start < current_offset:
                continue
            if lines_consumed >= len(entries):
                break
            if not line.strip():
                continue

            entry = entries[lines_consumed]
            filename = Path(entry["path"]).name
            s3_key = (
                f"{self.project}/{self.study}/{self.trial_id}/{entry['key']}/{filename}"
            )
            self.upload_fn(s3_key, entry["path"])

            new_offset = start + len(line) + 1
            manifest.advance_cursor(new_offset)
            lines_consumed += 1
