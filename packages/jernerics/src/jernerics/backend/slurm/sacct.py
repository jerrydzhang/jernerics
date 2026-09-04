import re
import subprocess
from dataclasses import dataclass
from functools import partial

from jernerics.backend.adapter import JobResourceSnapshot

SACCT_TIMEOUT_S = 10.0
"""Wall-clock ceiling for one sacct invocation."""

SACCT_FORMAT = (
    "JobIDRaw,State,ExitCode,Elapsed,AllocCPUS,ReqMem,MaxRSS,AveRSS,"
    "%CPU,CPUTime,AllocTRES,NodeList"
)
"""Column order of the parsable2 query; parse_sacct_output maps by position."""

_STEP_ROW = re.compile(r"\.(batch|extern|[0-9]+)$")

_MEM_SCALE = {"K": 1024.0, "M": 1024.0**2, "G": 1024.0**3, "T": 1024.0**4}
_MEM_BYTES = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT])$", re.IGNORECASE)
_REQ_MEM = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT])\s*[cn]?$", re.IGNORECASE)


@dataclass(frozen=True)
class SacctResult:
    """Outcome of one accounting query: a snapshot or an error string."""

    snapshot: JobResourceSnapshot | None
    error: str | None


def parse_duration_s(value: str) -> float | None:
    """[[DD-]HH:]MM:SS or plain seconds to seconds; None when unparseable."""
    text = value.strip()
    if not text:
        return None
    days = 0.0
    if "-" in text:
        day_part, _, rest = text.partition("-")
        if not day_part.isdigit() or not rest:
            return None
        days = float(day_part)
        text = rest
    parts = text.split(":")
    if len(parts) > 3:
        return None
    if len(parts) == 1:
        try:
            return days * 86_400.0 + float(text)
        except ValueError:
            return None
    if not all(part.isdigit() for part in parts):
        return None
    seconds = float(parts[-1]) + 60.0 * float(parts[-2])
    if len(parts) == 3:
        seconds += 3_600.0 * float(parts[-3])
    return days * 86_400.0 + seconds


def parse_mem_mb(value: str) -> float | None:
    """K/M/G/T-scaled sacct memory to MiB; None when unparseable."""
    match = _MEM_BYTES.match(value.strip())
    if match is None:
        return None
    return float(match.group(1)) * _MEM_SCALE[match.group(2).upper()] / 1024.0**2


def parse_req_mem(value: str) -> str | None:
    """ReqMem with its per-cpu/per-node modifier dropped: '16Gc' -> '16G'."""
    match = _REQ_MEM.match(value.strip())
    if match is None:
        return None
    return f"{match.group(1)}{match.group(2).upper()}"


def parse_percent(value: str) -> float | None:
    """sacct percentage like '98.54%' to a bare float; None when unparseable."""
    try:
        return float(value.strip().removesuffix("%"))
    except ValueError:
        return None


def _optional_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


def _optional_text(value: str) -> str | None:
    text = value.strip()
    return text or None


def _column(fields: list[str], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def parse_sacct_output(job_id: str, stdout: str) -> JobResourceSnapshot | None:
    """First non-step row of parsable2 sacct output as a snapshot.

    Step and auxiliary rows (``.batch``, ``.extern``, ``.<step>``) repeat
    the allocation's facts per component and are stripped; the requested
    job id labels the snapshot even when the surviving row is an array
    element. Returns None when no row survives.
    """
    for line in stdout.splitlines():
        fields = line.split("|")
        if len(fields) < 2:
            continue
        row_id = fields[0].strip()
        if not row_id or _STEP_ROW.search(row_id):
            continue

        at = partial(_column, fields)

        return JobResourceSnapshot(
            job_id=job_id,
            state=_optional_text(at(1)),
            exit_code=_optional_text(at(2)),
            wall_time_s=parse_duration_s(at(3)),
            cpu_pct=parse_percent(at(8)),
            cpu_time_s=parse_duration_s(at(9)),
            alloc_cpus=_optional_int(at(4)),
            req_mem=parse_req_mem(at(5)),
            max_rss_mb=parse_mem_mb(at(6)),
            ave_rss_mb=parse_mem_mb(at(7)),
            alloc_tres=_optional_text(at(10)),
            node_list=_optional_text(at(11)),
        )
    return None


def fetch_job_resources(
    job_id: str, *, timeout: float = SACCT_TIMEOUT_S
) -> SacctResult:
    """Run the sacct accounting query for one job; never raises on failure."""
    try:
        result = subprocess.run(
            [
                "sacct",
                "-n",
                "-P",
                "-j",
                job_id,
                f"--format={SACCT_FORMAT}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SacctResult(
            None, f"sacct for job {job_id} timed out after {timeout:.0f}s"
        )
    except OSError as error:
        return SacctResult(None, f"sacct unavailable for job {job_id}: {error}")
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[0] if detail else f"exit code {result.returncode}"
        return SacctResult(None, f"sacct for job {job_id} failed: {reason}")
    snapshot = parse_sacct_output(job_id, result.stdout)
    if snapshot is None:
        return SacctResult(
            None,
            f"sacct returned no accounting row for job {job_id} "
            "(job may be too fresh for the accounting database)",
        )
    return SacctResult(snapshot, None)
