"""ffmpeg encode command builder: NVENC fast mode and x265 quality mode.

Targets the project spec: ~2 GB per hour of video (Blu-ray 1080p), ~1 GB
per hour for DVD-native SD, AAC 5.1 audio, English subs as sidecar SRT.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["EncodePlan", "build_encode_args", "estimate_video_bitrate_kbps"]

GB = 1_000_000_000

# Container overhead + muxing slack, fraction of total budget.
CONTAINER_SLACK = 0.03

# AAC bitrate by channel count.
AUDIO_KBPS = {8: 640, 6: 448, 2: 192, 1: 128}

# Default size budgets (GB per hour of video).
# v0.2.16 (Eric decision, 2026-10-02 15:05): DVD target 1.5 -> 1.0.
BLURAY_GB_PER_HOUR = 2.0
DVD_GB_PER_HOUR = 1.0


def estimate_video_bitrate_kbps(
    *,
    target_gb_per_hour: float = BLURAY_GB_PER_HOUR,
    audio_channels: int = 6,
    audio_kbps: int | None = None,
) -> int:
    """Video bitrate (kbit/s) that hits the size budget.

    X GB/hour is a fixed average bitrate, independent of duration:
    X GB/hour * 8e9/1000 kbit per GB / 3600 s = X * 2222 kbit/s total,
    minus the audio track and container slack.
    """
    audio = audio_kbps or AUDIO_KBPS.get(min(audio_channels, 8), 448)
    avg_total_kbps = target_gb_per_hour * GB * 8 / 1000 / 3600
    avg_total_kbps *= 1 - CONTAINER_SLACK
    return max(int(avg_total_kbps - audio), 500)


@dataclass
class EncodePlan:
    """One ffmpeg encode job for one output file."""

    input_path: str
    output_path: str
    duration_seconds: int
    media_type: str  # 'bluray' | 'dvd'
    encoder: str  # 'nvenc' | 'x265'
    video_input_path: str | None = None
    audio_sid: int | None = None
    subtitle_sid: int | None = None
    target_gb_per_hour: float | None = None  # None -> default by media type
    audio_kbps: int | None = None
    audio_channels: int = 6
    deinterlace: bool = False
    extra_args: list[str] = field(default_factory=list)

    @property
    def is_sd(self) -> bool:
        return self.media_type == "dvd"

    @property
    def gb_per_hour(self) -> float:
        if self.target_gb_per_hour is not None:
            return self.target_gb_per_hour
        return DVD_GB_PER_HOUR if self.is_sd else BLURAY_GB_PER_HOUR

    @property
    def target_video_kbps(self) -> int:
        return estimate_video_bitrate_kbps(
            target_gb_per_hour=self.gb_per_hour,
            audio_channels=self.audio_channels,
            audio_kbps=self.audio_kbps,
        )


def build_encode_args(plan: EncodePlan) -> list[str]:
    """Full ffmpeg argument list for one encode (without the binary name)."""
    args = ["-y", "-hide_banner", "-nostdin"]
    if plan.video_input_path:
        args += ["-i", plan.video_input_path, "-i", plan.input_path]
        video_input = "0"
        audio_input = "1"
    else:
        args += ["-i", plan.input_path]
        video_input = "0"
        audio_input = "0"

    # Audio: encode to AAC at the plan's bitrate.
    # TODO(next milestone): resolve audio_sid against the ripped MKV's real
    # stream layout via ffprobe (MakeMKV renumbers streams in the MKV).
    if plan.audio_sid is not None:
        args += ["-map", f"{audio_input}:a:{plan.audio_sid}?"]
    else:
        args += ["-map", f"{audio_input}:a:0?"]
    a_kbps = plan.audio_kbps or AUDIO_KBPS.get(min(plan.audio_channels, 8), 448)
    args += ["-c:a", "aac", "-b:a", f"{a_kbps}k"]

    # Video filters: deinterlace 1080i/interlaced sources, scale Blu-ray to 1080p.
    # DVDs stay at native SD resolution (no pointless upscaling).
    filters: list[str] = []
    if plan.deinterlace:
        filters.append("yadif")
    if not plan.is_sd:
        filters.append("scale=-2:1080:flags=lanczos")

    v_kbps = plan.target_video_kbps
    if plan.encoder == "nvenc":
        args += [
            "-map", f"{video_input}:v:0",
            "-c:v", "hevc_nvenc",
            "-preset", "p6",
            "-rc", "vbr",
            "-b:v", f"{v_kbps}k",
            "-maxrate", f"{int(v_kbps * 1.45)}k",
            "-bufsize", f"{v_kbps * 2}k",
            "-spatial-aq", "1",
            "-temporal-aq", "1",
        ]
    else:  # x265 quality mode
        args += [
            "-map", f"{video_input}:v:0",
            "-c:v", "libx265",
            "-preset", "slow",
            "-b:v", f"{v_kbps}k",
            "-x265-params",
            f"keyint=240:min-keyint=24:vbv-maxrate={int(v_kbps * 1.3)}:vbv-bufsize={v_kbps * 2}",
        ]
    if filters:
        args += ["-vf", ",".join(filters)]

    args += ["-movflags", "+faststart", "-f", "mp4", plan.output_path]
    return args