from pathlib import Path

import pytest

from bluray_to_mp4 import upscale as upscale_mod
from bluray_to_mp4.upscale import UpscaleError, UpscalePlan, run_dvd_upscale


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def test_upscale_uses_2x_model_forced_tile_and_direct_segment(monkeypatch, tmp_path):
    calls = []

    # Resolve to a distinct path so the "binary not found" guard in
    # run_dvd_upscale (which fires when the resolver returns the requested
    # name unchanged) does not reject the stubbed resolution.
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "realesrgan-ncnn-vulkan-stub"),
    )
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: binary)
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path),
    )

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    output = tmp_path / "title_00_720p.mkv"
    result = run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(output),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=2,
        )
    )

    # v0.2.33: the stage returns (mezzanine_path, total_frames).
    assert result == (str(output), 2)
    extract_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale frame extraction")
    )
    # v0.2.33: the prepare filter is appended AFTER select=between(n,...)
    # (design §2.4 - deinterlace/geometry run on the selected frames).
    assert extract_cmd[extract_cmd.index("-vf") + 1] == (
        "select='between(n,0,1)',"
        "scale=w='trunc(360*dar/2)*2':h=360:flags=lanczos,setsar=1"
    )

    esrgan_cmd = next(cmd for label, cmd in calls if label.startswith("Real-ESRGAN"))
    # v0.2.28: the live-action profile runs the bundled native-2x
    # realesrgan-x2plus (NOT x4plus-at--s 2, which corrupted frames in
    # v0.2.25 - x2plus is a true scale-2 net, so -s 2 is its native mode).
    assert esrgan_cmd[esrgan_cmd.index("-n") + 1] == "realesrgan-x2plus"
    assert esrgan_cmd[esrgan_cmd.index("-s") + 1] == "2"
    # 640x360 frames force a single tile: extent 640 + 64 margin = 704
    # (the forced single tile must hold for x2plus exactly as it did for
    # animevideov3 - auto tiling reintroduces the v0.2.23 seam).
    assert esrgan_cmd[esrgan_cmd.index("-t") + 1] == "704"

    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    # 2x lands directly at 1280x720; the segment pass only stamps SAR.
    assert segment_cmd[segment_cmd.index("-vf") + 1] == "setsar=1"


# ---------------------------------------------------------------------
# v0.2.28 profile mapping: live-action 720p -> realesrgan-x2plus (the
# bundled verified conversion of the official RealESRGAN_x2plus weights,
# a TRUE native-2x net); anime keeps realesr-animevideov3.

def test_live_action_profile_maps_to_native_2x_x2plus():
    """720p runs realesrgan-x2plus at -s 2. That pairing is the model's
    native mode (a genuine scale-2 RRDBNet) - categorically different
    from v0.2.25's realesrgan-x4plus at -s 2, which corrupted frames
    because a 4x net cannot be run at 2x."""
    assert upscale_mod.model_name_for_mode("720p") == "realesrgan-x2plus"
    assert upscale_mod.realesrgan_scale_for_mode("720p") == 2


def test_anime_profile_maps_to_animevideov3_at_2x():
    """The anime profile is unchanged since v0.2.26: realesr-animevideov3
    at its native 2x."""
    assert upscale_mod.model_name_for_mode("720p_anime") == "realesr-animevideov3"
    assert upscale_mod.realesrgan_scale_for_mode("720p_anime") == 2


def test_unknown_upscale_mode_is_rejected():
    for fn in (upscale_mod.model_name_for_mode, upscale_mod.realesrgan_scale_for_mode):
        with pytest.raises(UpscaleError, match="unknown DVD upscale mode"):
            fn("1080p")


def test_models_resolver_requires_bundled_x2plus(tmp_path):
    """v0.2.28: a models folder missing realesrgan-x2plus must fail at
    resolve time with the file named - loudly, never at Real-ESRGAN
    runtime mid-disc."""
    models = tmp_path / "models"
    models.mkdir()
    for name in (
        "realesrgan-x4plus.param",
        "realesrgan-x4plus.bin",
        "realesr-animevideov3-x2.param",
        "realesr-animevideov3-x2.bin",
    ):
        (models / name).write_text("fake model")
    with pytest.raises(UpscaleError, match="realesrgan-x2plus"):
        upscale_mod.resolve_realesrgan_models("realesrgan-ncnn-vulkan", str(models))


def test_probe_geometry_does_not_change_2x_engine_path(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "realesrgan-ncnn-vulkan-stub"),
    )
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: binary)
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path),
    )

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=2,
            source_width=720,
            source_height=480,
            display_aspect_ratio="16:9",
            sample_aspect_ratio="32:27",
        )
    )

    extract_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale frame extraction")
    )
    # v0.2.33: the prepare filter is appended AFTER select=between(n,...)
    # (design §2.4 - deinterlace/geometry run on the selected frames).
    assert extract_cmd[extract_cmd.index("-vf") + 1] == (
        "select='between(n,0,1)',"
        "scale=w='trunc(360*dar/2)*2':h=360:flags=lanczos,setsar=1"
    )

    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    assert segment_cmd[segment_cmd.index("-vf") + 1] == "setsar=1"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, "704"), (512, "704"), (1024, "1024"), (4096, "4096")],
)
def test_tile_size_covers_frame_without_forcing_2048(monkeypatch, tmp_path, requested, expected):
    seen = []

    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "realesrgan-ncnn-vulkan-stub"),
    )
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: binary)
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path),
    )

    def fake_run(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            seen.append(cmd)
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=2,
            tile_size=requested,
        )
    )

    assert seen[0][seen[0].index("-s") + 1] == "2"
    assert seen[0][seen[0].index("-t") + 1] == expected


def test_tile_argument_never_uses_auto_tiling(monkeypatch, tmp_path):
    """The command must never request -t 0.

    Auto tiling let the Vulkan driver split frames into a tile grid, which
    reintroduced the v0.2.23 seam-line artifact in v0.2.25.
    """
    seen = []

    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "realesrgan-ncnn-vulkan-stub"),
    )
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: binary)
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path),
    )

    def fake_run(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            seen.append(cmd)
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=2,
        )
    )

    tile = seen[0][seen[0].index("-t") + 1]
    assert tile != "0"
    assert int(tile) >= 640 + 64


def test_anime_profile_keeps_anime_model_at_2x(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "realesrgan-ncnn-vulkan-stub"),
    )
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: binary)
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path),
    )

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p_anime",
            duration_seconds=1,
            total_frames=2,
        )
    )

    esrgan_cmd = next(cmd for label, cmd in calls if label.startswith("Real-ESRGAN"))
    assert esrgan_cmd[esrgan_cmd.index("-n") + 1] == "realesr-animevideov3"
    assert esrgan_cmd[esrgan_cmd.index("-s") + 1] == "2"
    assert esrgan_cmd[esrgan_cmd.index("-t") + 1] == "704"

    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    assert segment_cmd[segment_cmd.index("-vf") + 1] == "setsar=1"


# ---------------------------------------------------------------------
# v0.2.27: fps desync fix (run 13). The mezzanine writer stamps every
# reassembled chunk with plan.fps; the plan previously hard-coded
# 29.97 while MakeMKV strips pulldown (film DVD rips are 23.976), so
# each concatenated segment drifted ~20%. The plan now carries the
# rip's probed frame clock, and a duration gate refuses a mezzanine
# that drifted from the rip.

def _stub_upscale_internals(monkeypatch, tmp_path):
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "realesrgan-ncnn-vulkan-stub"),
    )
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: binary)
    monkeypatch.setattr(
        upscale_mod,
        "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path),
    )


def _recording_run_process(calls, count=2):
    """Fake _run_process for run_dvd_upscale tests.

    v0.2.33: extraction writes exactly `count` PNGs per chunk (the
    per-chunk frame-count assertion must hold) starting at the chunk's
    -start_number, and the segment writer records the segment file.
    """

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(count):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")

    return fake_run


def test_mezzanine_command_carries_probed_fps(monkeypatch, tmp_path):
    """The mezzanine segment writer stamps -framerate with the PLAN's
    fps, so a probed 23.976 film rip reassembles on its own clock (the
    run-13 fix) instead of the hardcoded 29.97 stamp."""
    calls = []
    _stub_upscale_internals(monkeypatch, tmp_path)
    monkeypatch.setattr(upscale_mod, "_run_process", _recording_run_process(calls))

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=2,
            fps="24000/1001",
        )
    )
    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    assert segment_cmd[segment_cmd.index("-framerate") + 1] == "24000/1001"


def test_mezzanine_command_default_fps_is_2997(monkeypatch, tmp_path):
    """Source-compat pin: a plan without an explicit fps still stamps
    the 30000/1001 default (the fallback the pipeline warns about)."""
    calls = []
    _stub_upscale_internals(monkeypatch, tmp_path)
    monkeypatch.setattr(upscale_mod, "_run_process", _recording_run_process(calls))

    plan = UpscalePlan(
        input_path=str(tmp_path / "input.mkv"),
        output_path=str(tmp_path / "out.mkv"),
        workdir=str(tmp_path / "work"),
        mode="720p",
        duration_seconds=1,
        total_frames=2,
    )
    assert plan.fps == "30000/1001"
    run_dvd_upscale(plan)
    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    assert segment_cmd[segment_cmd.index("-framerate") + 1] == "30000/1001"


# ---------------------------------------------------------------------
# v0.2.33 frame-exact gate (design §3): verify_mezzanine_frames replaces
# the duration gate. Exact INTEGER frame-count equality, no tolerance
# window - under frame-exact chunking the mezzanine contains the rip's
# frames by construction, so any drift mechanism produces a nonzero
# integer difference. The old duration-gate tests are retired with the
# function; the pinned Mamma Mia 15.05 s drift fixture FLIPS to a pass
# case (design §5 row 4): that shape cannot occur when counts match.

def test_frames_gate_rejects_mismatched_frame_count():
    """An off-by-one frame mismatch is refused with an UpscaleError
    naming BOTH counts - the pipeline then encodes the original rip,
    retains the mezzanine, and writes a diagnostic."""
    from bluray_to_mp4.upscale import verify_mezzanine_frames

    def probe(path, *, binary="ffprobe"):
        return 6071 if "mezz" in path else 6072

    with pytest.raises(UpscaleError, match="6071.*6072"):
        verify_mezzanine_frames("mezz.mkv", 6072, probe_frames=probe)


def test_frames_gate_accepts_exact_match():
    """Equal counts pass - even the Mamma Mia 15.05 s drift SHAPE is a
    pass case now, because under frame-exact reassembly equal frame
    counts cannot coexist with drift (the mezzanine is stamped at the
    rip's own verified CFR clock)."""
    from bluray_to_mp4.upscale import verify_mezzanine_frames

    # The old fixture: rip 6819.648s, mezz 6804.598s at the SAME clock
    # would imply different frame counts - refused. Equal counts pass.
    verify_mezzanine_frames(
        "mezz.mkv", 163_267, probe_frames=lambda p, *, binary="ffprobe": 163_267
    )


def test_frames_gate_accepts_mamma_mia_when_counts_match():
    """Rewritten from the v0.2.31 duration fixture (6804.598s vs
    6819.648s was a REJECT under the 0.5s+0.01% gate): the same disc
    under frame-exact chunking produces a mezzanine with EXACTLY the
    rip's 163_267 frames (the drift came from time-window chunking,
    not from the frames themselves), and the gate passes it."""
    from bluray_to_mp4.upscale import verify_mezzanine_frames

    verify_mezzanine_frames(
        "mezz.mkv", 163_267, probe_frames=lambda p, *, binary="ffprobe": 163_267
    )
    # And one frame short is still refused - exact equality only.
    with pytest.raises(UpscaleError, match="163266.*163267"):
        verify_mezzanine_frames(
            "mezz.mkv", 163_267, probe_frames=lambda p, *, binary="ffprobe": 163_266
        )


def test_frames_gate_rejects_unprobeable_mezzanine():
    """A mezzanine ffprobe cannot read cannot be trusted - refuse it
    (fail open to the original rip), never encode a blind guess."""
    from bluray_to_mp4.upscale import verify_mezzanine_frames

    def broken(path, *, binary="ffprobe"):
        raise RuntimeError("ffprobe failed")

    with pytest.raises(UpscaleError, match="could not be probed"):
        verify_mezzanine_frames("mezz.mkv", 7590, probe_frames=broken)


def test_frames_gate_rejects_unknown_rip_frame_count():
    """v0.2.29 semantics carried to frames (design §3): an unknown rip
    count (probe failed - effectively <= 0 or None) must NOT pass the
    gate; an unknown count cannot certify a mezzanine."""
    from bluray_to_mp4.upscale import verify_mezzanine_frames

    def probe(path, *, binary="ffprobe"):
        return 7590

    # Zero: the frame-count probe failed entirely.
    with pytest.raises(UpscaleError, match="rip frame count unknown"):
        verify_mezzanine_frames("mezz.mkv", 0, probe_frames=probe)
    # None (probe never ran) - same refusal.
    with pytest.raises(UpscaleError, match="rip frame count unknown"):
        verify_mezzanine_frames("mezz.mkv", None, probe_frames=probe)
    # Negative garbage is equally unknown.
    with pytest.raises(UpscaleError, match="rip frame count unknown"):
        verify_mezzanine_frames("mezz.mkv", -1, probe_frames=probe)


# ---------------------------------------------------------------------
# v0.2.34 clock gate (design §3): verify_mezzanine_clock composes with
# verify_mezzanine_frames on the TIME axis. The frames gate owns exactness
# (integer counts); this gate owns the CLOCK - the mezzanine's container
# duration must match the selected audio stream's measured extent within
# max(2 s floor, 0.1% relative). It kills the shipped-.33 failure shape:
# correct count + wrong stamped rate = wrong playback duration, which the
# count gate cannot see.

def _clock_probe(seconds: float):
    """probe_duration_seconds-shaped stub returning a fixed figure."""
    def probe(path, *, binary="ffprobe"):
        return seconds
    return probe


def test_clock_gate_passes_within_tolerance():
    """U1: the good Mamma Mia shape - mezzanine 6,819.7 s (163,509
    frames at 23.976) vs 6,818.4 s of audio; delta 1.3 s against a
    tolerance of max(2.0, 6.82) = 6.8 s. Passes with 5x headroom - the
    audio lead-in/out skew a correct stamp still carries is not a
    failure."""
    from bluray_to_mp4.upscale import verify_mezzanine_clock

    verify_mezzanine_clock(
        "mezz.mkv", 6818.4, probe_duration=_clock_probe(6819.7))


def test_clock_gate_rejects_rate_class_desync():
    """U2: the shipped-v0.2.33 failure shape - the same 163,509 frames
    stamped 29.97 play 5,453 s against 6,818.4 s of audio (delta 1,365 s,
    a rate-CLASS error 25% apart). The gate refuses with both durations
    and the tolerance named; the pipeline then fails open to the
    original rip with retention + diagnostic."""
    from bluray_to_mp4.upscale import verify_mezzanine_clock

    with pytest.raises(UpscaleError) as exc:
        verify_mezzanine_clock(
            "mezz.mkv", 6818.4, probe_duration=_clock_probe(5453.0))
    message = str(exc.value)
    assert "5453.0" in message and "6818.4" in message
    assert "6.8" in message  # the tolerance that was exceeded


def test_clock_gate_tolerance_scales_with_short_content():
    """U3: a 90-minute title's tolerance is max(2.0, 0.1% x 5,400) =
    5.4 s - the relative term scales down for short content while the
    2 s floor keeps tiny features gating sanely. Boundary behavior is
    pinned at exactly the tolerance (pass) and a hair past (fail).
    """
    from bluray_to_mp4.upscale import verify_mezzanine_clock

    audio = 5400.0
    verify_mezzanine_clock(
        "mezz.mkv", audio, probe_duration=_clock_probe(audio + 5.4))
    verify_mezzanine_clock(
        "mezz.mkv", audio, probe_duration=_clock_probe(audio - 5.4))
    with pytest.raises(UpscaleError):
        verify_mezzanine_clock(
            "mezz.mkv", audio, probe_duration=_clock_probe(audio + 5.5))
    # Short content rides the 2 s floor, not the relative term.
    verify_mezzanine_clock("mezz.mkv", 60.0, probe_duration=_clock_probe(61.5))
    with pytest.raises(UpscaleError):
        verify_mezzanine_clock("mezz.mkv", 60.0, probe_duration=_clock_probe(63.0))


def test_clock_gate_rejects_unprobeable_mezzanine():
    """U4: a mezzanine whose duration ffprobe cannot read cannot be
    clock-verified - refuse it honestly (same shape as the frames
    gate's unprobeable refusal), never encode a blind guess."""
    from bluray_to_mp4.upscale import verify_mezzanine_clock

    def broken(path, *, binary="ffprobe"):
        raise RuntimeError("ffprobe failed")

    with pytest.raises(UpscaleError, match="could not be probed"):
        verify_mezzanine_clock("mezz.mkv", 6818.4, probe_duration=broken)


def test_clock_gate_rejects_unknown_audio_duration():
    """Companion to U4 on the other operand: an unknown audio extent
    (probe failed upstream - <= 0 or None) cannot certify a clock;
    the gate refuses exactly like an unknown rip frame count (v0.2.29
    semantics carried to the clock axis)."""
    from bluray_to_mp4.upscale import verify_mezzanine_clock

    with pytest.raises(UpscaleError, match="audio duration unknown"):
        verify_mezzanine_clock(
            "mezz.mkv", 0.0, probe_duration=_clock_probe(6818.4))
    with pytest.raises(UpscaleError, match="audio duration unknown"):
        verify_mezzanine_clock(
            "mezz.mkv", None, probe_duration=_clock_probe(6818.4))


# ---------------------------------------------------------------------
# v0.2.33 frame math (design §5 rows 1-3): chunk frames at odd fps,
# last-chunk remainder, and boundary alignment of the extraction cmd.

def test_chunk_math_24000_1001():
    """Design §5 row 1: fps=24000/1001, total=10000, chunk_seconds=120
    -> F = round(120*24000/1001) = 2877, chunks = ceil(10000/2877) = 4,
    remainder = 10000 - 3*2877 = 1369."""
    from bluray_to_mp4.upscale import (
        compute_chunk_bounds,
        compute_chunk_frames,
    )

    F = compute_chunk_frames(120, "24000/1001")
    assert F == 2877
    bounds = compute_chunk_bounds(10000, F)
    firsts = [first for first, _ in bounds]
    counts = [count for _, count in bounds]
    assert len(bounds) == 4
    # Row 2: per-chunk counts are [2877, 2877, 2877, 1369].
    assert counts == [2877, 2877, 2877, 1369]
    # Boundaries align: chunks span [i*F, (i+1)*F).
    assert firsts == [0, 2877, 5754, 8631]
    assert sum(counts) == 10000


def test_extraction_uses_frame_index_select(monkeypatch, tmp_path):
    """Design §5 row 3: the extraction command addresses decoded frame
    indices via select='between(n,first,last)' - chunk 2 of the same
    plan is between(n,2877,5753) - and contains NO -ss/-t; -vsync 0 is
    present (passthrough so ffmpeg never dup/drop to hit a clock)."""
    calls = []
    _stub_upscale_internals(monkeypatch, tmp_path)

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            # Write exactly the frames this chunk must yield.
            between = cmd[cmd.index("-vf") + 1].split("between(n,")[1]
            first = int(between.split(",")[0])
            last = int(between.split(",")[1].split(")")[0])
            for i in range(first, last + 1):
                (in_dir / f"frame_{start_number + (i - first):08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")
        elif label.startswith("upscale mezzanine concat"):
            # The concat writes the final mezzanine from the segment list.
            Path(cmd[-1]).write_bytes(b"mezzanine")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=10000,
            fps="24000/1001",
        )
    )

    extract_cmds = [
        cmd for label, cmd in calls if label.startswith("upscale frame extraction")
    ]
    assert len(extract_cmds) == 4
    # Chunk 2: frames [2877, 5754) -> between(n,2877,5753).
    vf2 = extract_cmds[1][extract_cmds[1].index("-vf") + 1]
    assert "select='between(n,2877,5753)'" in vf2
    # No seek, no time window (frame-index extraction, §2.4).
    assert "-ss" not in extract_cmds[1]
    assert "-t" not in extract_cmds[1]
    assert "-vsync" in extract_cmds[1]
    assert extract_cmds[1][extract_cmds[1].index("-vsync") + 1] == "0"
    # -start_number continues the global PNG numbering.
    assert extract_cmds[1][extract_cmds[1].index("-start_number") + 1] == "2878"
    # Last chunk is the remainder: between(n,8631,9999).
    vf_last = extract_cmds[3][extract_cmds[3].index("-vf") + 1]
    assert "select='between(n,8631,9999)'" in vf_last
    # v0.2.33: the segment reader must be told where this chunk's PNG
    # numbering starts (extraction wrote global numbering, not 1-based
    # per chunk) - otherwise chunks > 0 fail to open their input.
    seg_cmds = [
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    ]
    assert seg_cmds[1][seg_cmds[1].index("-start_number") + 1] == "2878"


def test_run_dvd_upscale_requires_total_frames(monkeypatch, tmp_path):
    """Design §2.3: a plan without a probed total_frames refuses before
    any chunk runs - the pre-flight owns the count, never the stage."""
    _stub_upscale_internals(monkeypatch, tmp_path)
    with pytest.raises(UpscaleError, match="rip frame count unknown"):
        run_dvd_upscale(
            UpscalePlan(
                input_path=str(tmp_path / "input.mkv"),
                output_path=str(tmp_path / "out.mkv"),
                workdir=str(tmp_path / "work"),
                mode="720p",
                duration_seconds=1,
            )
        )


def test_per_chunk_extraction_count_mismatch_raises(monkeypatch, tmp_path):
    """Design §2.4 per-chunk assertion: extraction returning the wrong
    frame count fails the chunk loudly instead of building a short
    mezzanine."""
    calls = []
    _stub_upscale_internals(monkeypatch, tmp_path)

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            # Off-by-one: one frame short of the requested window.
            last = cmd[cmd.index("-vf") + 1]
            first = int(last.split("between(n,")[1].split(",")[0])
            for i in range(first, first + 3):  # plan asked for 4
                (in_dir / f"frame_{start_number + (i - first):08d}.png").write_bytes(
                    png_header(640, 360)
                )

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)
    with pytest.raises(UpscaleError, match="extracted 3"):
        run_dvd_upscale(
            UpscalePlan(
                input_path=str(tmp_path / "input.mkv"),
                output_path=str(tmp_path / "out.mkv"),
                workdir=str(tmp_path / "work"),
                mode="720p",
                duration_seconds=1,
                total_frames=4,
            )
        )


def test_per_chunk_upscale_count_mismatch_raises(monkeypatch, tmp_path):
    """Design §2.4 second assertion: Real-ESRGAN must return exactly the
    frames it was handed; a short output fails the chunk."""
    calls = []
    _stub_upscale_internals(monkeypatch, tmp_path)

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(3):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            # Drop one upscaled frame.
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png"))[:-1]:
                (out_dir / frame.name).write_bytes(png_header(1280, 720))

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)
    with pytest.raises(UpscaleError, match="upscaled 2"):
        run_dvd_upscale(
            UpscalePlan(
                input_path=str(tmp_path / "input.mkv"),
                output_path=str(tmp_path / "out.mkv"),
                workdir=str(tmp_path / "work"),
                mode="720p",
                duration_seconds=1,
                total_frames=3,
            )
        )


# =====================================================================
# v0.2.35: finalize-phase heartbeat (design §2) + per-chunk resume
# (design §3). All synthetic; probes and processes are stubbed per the
# suite convention.

import json as _json
import time

from bluray_to_mp4.upscale import (
    FINALIZE_STEP_CONCAT,
    FINALIZE_STEPS,
    ProgressHeartbeat,
    compute_chunk_bounds,
    compute_chunk_frames,
)
from bluray_to_mp4.makemkv import EncodeCancelled


def _fake_probe_result(*, frames, rate="24000/1001"):
    class _Result:
        returncode = 0
        stderr = ""
        stdout = _json.dumps({
            "streams": [{
                "nb_read_frames": str(frames),
                "r_frame_rate": rate,
            }],
        })

    return _Result()


def _stub_segment_probe(monkeypatch, *, good=True, frames=2,
                        rate="24000/1001", fail=False):
    """Stub _verify_segment's ffprobe probe. good=False still returns a
    well-formed probe whose numbers mismatch the plan (count/fps lie);
    fail=True simulates a probe crash (nonzero exit)."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if fail:
            class _Bad:
                returncode = 1
                stdout = ""
                stderr = "boom"
            return _Bad()
        if good:
            return _fake_probe_result(frames=frames, rate=rate)
        return _fake_probe_result(frames=frames, rate="30000/1001")

    monkeypatch.setattr(upscale_mod.subprocess, "run", fake_run)
    return calls


def _write_manifest(workroot, **overrides):
    payload = {
        "version": 1,
        "input_path": "/x/input.mkv",
        "mode": "720p",
        "binary_sha_basename": {
            "binary": "/bin/realesrgan-ncnn-vulkan-stub",
            "model": "realesrgan-x2plus",
        },
        "fps": "24000/1001",
        "total_frames": 8,
        "chunk_frames": 2,
        "deinterlace": False,
        "target_height": 720,
        "segment_filter": "setsar=1",
        "esrgan_scale": 2,
        "attempt_started_at": "2026-10-19T12:00:00",
    }
    payload.update(overrides)
    workroot.mkdir(parents=True, exist_ok=True)
    (workroot / "resume_manifest.json").write_text(
        _json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


class _Clock:
    """Deterministic clock for heartbeat tests (design §5: 'controllable
    clock; heartbeat interval patched to 0.05 s')."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


def _finalize_capable_run(monkeypatch, tmp_path, *, on_progress,
                          total_frames=2, cancel=None, concat_hook=None,
                          workroot_extra=None):
    """A one-chunk run whose extraction/upscale/segment steps are
    stubbed; the concat path calls concat_hook so tests can stretch the
    wall clock across finalize."""
    _stub_upscale_internals(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for i in range(2):
                (out_dir / f"frame_{1 + i:08d}.png").write_bytes(
                    png_header(1280, 720)
                )
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")
        elif label.startswith("upscale mezzanine concat"):
            Path(cmd[-1]).write_bytes(b"mezzanine")
            if concat_hook is not None:
                concat_hook()
    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)
    return calls


def test_finalize_heartbeat_pings_during_concat(monkeypatch, tmp_path):
    """U-A1: the heartbeat pings on_progress every interval through the
    concat block; pings carry the clamped 0.999 frac, the
    'concatenating mezzanine' step, and heartbeat=True, and the
    emission before the block carries the same step label."""
    events = []

    def slow_concat():
        # Advance the fake clock in ~interval steps inside the concat
        # subprocess so the heartbeat thread fires >= 2 times.
        for _ in range(6):
            time.sleep(0.06)

    _finalize_capable_run(
        monkeypatch, tmp_path,
        on_progress=None,  # unused by helper path; events via closure
        concat_hook=slow_concat,
    )

    captured = []

    def on_progress(frac, eta, detail=None):
        captured.append((frac, eta, detail))

    monkeypatch.setattr(
        upscale_mod, "_FINALIZE_HEARTBEAT_SECONDS", 0.05)

    # Re-run with the capturing on_progress: total_frames=2 -> one chunk
    # -> the shutil.move path; use the concat path with 2 chunks.
    _stub_upscale_internals(monkeypatch, tmp_path)

    def fake_run(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            vf = cmd[cmd.index("-vf") + 1]
            between = vf.split("between(n,")[1]
            first = int(between.split(",")[0])
            last = int(between.split(",")[1].split(")")[0])
            for i in range(first, last + 1):
                (in_dir / f"frame_{start_number + (i - first):08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")
        elif label.startswith("upscale mezzanine concat"):
            Path(cmd[-1]).write_bytes(b"mezzanine")
            for _ in range(3):
                time.sleep(0.07)

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=4,
            fps="24000/1001",
            chunk_frames=2,
        ),
        on_progress=on_progress,
    )

    finalize_events = [e for e in captured
                       if e[2] and e[2].get("step") == FINALIZE_STEP_CONCAT]
    assert finalize_events, "concat step must be announced"
    # The emission (not a ping) carries no heartbeat key and clamps frac.
    announce = finalize_events[0]
    assert announce[0] == pytest.approx(0.999)
    assert "heartbeat" not in announce[2]
    # The pings: clamped frac, same step, heartbeat=True, eta None.
    pings = finalize_events[1:]
    assert len(pings) >= 2, f"expected >=2 heartbeat pings, saw {len(pings)}"
    for frac, eta, detail in pings:
        assert frac == pytest.approx(0.999)
        assert eta is None
        assert detail["heartbeat"] is True
        assert detail["step"] == FINALIZE_STEP_CONCAT
        assert detail["chunks"] == detail["chunk"] == 2


def test_heartbeat_stops_cleanly_on_cancel_and_success(monkeypatch, tmp_path):
    """U-A2: the heartbeat thread never leaks - success joins it, and a
    cancel raised inside concat propagates EncodeCancelled with the
    thread joined too (no live b2m-finalize-heartbeat afterwards)."""
    import threading as _threading

    monkeypatch.setattr(
        upscale_mod, "_FINALIZE_HEARTBEAT_SECONDS", 0.05)
    _stub_upscale_internals(monkeypatch, tmp_path)

    def boom(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")
        elif label.startswith("upscale mezzanine concat"):
            time.sleep(0.09)
            raise EncodeCancelled("cancelled during concat")

    monkeypatch.setattr(upscale_mod, "_run_process", boom)

    before = {t.name for t in _threading.enumerate()}
    with pytest.raises(EncodeCancelled):
        run_dvd_upscale(
            UpscalePlan(
                input_path=str(tmp_path / "input.mkv"),
                output_path=str(tmp_path / "out.mkv"),
                workdir=str(tmp_path / "work"),
                mode="720p",
                duration_seconds=1,
                total_frames=4,
                fps="24000/1001",
                chunk_frames=2,
            ),
            on_progress=lambda *a, **k: None,
        )
    time.sleep(0.2)
    # The heartbeat thread is gone after the cancel path (joined in
    # the finally) - no leak into the next run.
    assert not any(
        t.name == "b2m-finalize-heartbeat" and t.is_alive()
        for t in _threading.enumerate()
    )

    # Success path: run to completion; the thread must be joined too.
    def calm(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")
        elif label.startswith("upscale mezzanine concat"):
            Path(cmd[-1]).write_bytes(b"mezzanine")

    monkeypatch.setattr(upscale_mod, "_run_process", calm)
    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "ok.mkv"),
            workdir=str(tmp_path / "work2"),
            mode="720p",
            duration_seconds=1,
            total_frames=4,
            fps="24000/1001",
            chunk_frames=2,
        ),
        on_progress=lambda *a, **k: None,
    )
    time.sleep(0.2)
    assert not any(
        t.name == "b2m-finalize-heartbeat" and t.is_alive()
        for t in _threading.enumerate()
    )


def test_heartbeat_never_sets_eta(monkeypatch, tmp_path):
    """U-A3: every finalize emission and every heartbeat ping carries
    eta=None - _forecast_total owns time_remaining; a heartbeat ETA
    would flash a bogus estimate on the card (6ca6216 lesson)."""
    etas = []

    def on_progress(frac, eta, detail=None):
        etas.append(eta)

    monkeypatch.setattr(
        upscale_mod, "_FINALIZE_HEARTBEAT_SECONDS", 0.05)
    _stub_upscale_internals(monkeypatch, tmp_path)

    def fake_run(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            for i in range(2):
                (in_dir / f"frame_{start_number + i:08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1]
                           ) if "-o" in cmd else None
            if out_dir is None:
                return
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment")
        elif label.startswith("upscale mezzanine concat"):
            Path(cmd[-1]).write_bytes(b"mezzanine")
            for _ in range(3):
                time.sleep(0.07)

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)
    run_dvd_upscale(
        UpscalePlan(
            input_path=str(tmp_path / "input.mkv"),
            output_path=str(tmp_path / "out.mkv"),
            workdir=str(tmp_path / "work"),
            mode="720p",
            duration_seconds=1,
            total_frames=4,
            fps="24000/1001",
            chunk_frames=2,
        ),
        on_progress=on_progress,
    )
    assert etas, "expected progress events"
    assert all(e is None for e in etas)


# ---------------------------------------------------------------------
# Feature B: per-chunk resume (design §3). The default plan everywhere
# below: fps=24000/1001, chunk_frames=2, total_frames=8 -> 4 chunks.

def _resume_plan(tmp_path, *, mode="720p", total_frames=8):
    return UpscalePlan(
        input_path=str(tmp_path / "input.mkv"),
        output_path=str(tmp_path / "out.mkv"),
        workdir=str(tmp_path / "work"),
        mode=mode,
        duration_seconds=1,
        total_frames=total_frames,
        fps="24000/1001",
        chunk_seconds=120,
        chunk_frames=2,
    )


def _resume_env(monkeypatch, tmp_path):
    """Stubbed binaries + a counting fake _run_process that fakes the
    full pipeline for whichever chunk it is asked to compute."""
    _stub_upscale_internals(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            vf = cmd[cmd.index("-vf") + 1]
            between = vf.split("between(n,")[1]
            first = int(between.split(",")[0])
            last = int(between.split(",")[1].split(")")[0])
            for i in range(first, last + 1):
                (in_dir / f"frame_{start_number + (i - first):08d}.png").write_bytes(
                    png_header(640, 360)
                )
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"segment-payload")
        elif label.startswith("upscale mezzanine concat"):
            Path(cmd[-1]).write_bytes(b"mezzanine")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)
    return calls


def _seed_segments(workroot, count=4, payload=b"segment-payload"):
    """chunk_0000..chunk_{count-1}/segment.mkv files, all 'complete'."""
    for idx in range(count):
        chunk = workroot / f"chunk_{idx:04d}"
        chunk.mkdir(parents=True, exist_ok=True)
        (chunk / "segment.mkv").write_bytes(payload)


def _stub_manifest_probe_ok(monkeypatch):
    """probe answers a matching count (2) at the plan fps for every
    segment; the manifest fingerprint must ALSO match (test seeds it)."""
    def fake_run(cmd, **kw):
        return _fake_probe_result(frames=2, rate="24000/1001")
    monkeypatch.setattr(upscale_mod.subprocess, "run", fake_run)


def _manifest_for(tmp_path, **overrides):
    workroot = tmp_path / "work"
    # Mirror _resume_manifest_payload exactly (sorted keys; the
    # attempt_started_at key is informational and ignored in compare).
    binary = str(tmp_path / "realesrgan-ncnn-vulkan-stub")
    payload = {
        "version": 1,
        "input_path": str(tmp_path / "input.mkv"),
        "mode": "720p",
        "binary_sha_basename": {"binary": binary, "model": "realesrgan-x2plus"},
        "fps": "24000/1001",
        "total_frames": 8,
        "chunk_frames": 2,
        "deinterlace": False,
        "target_height": 720,
        "segment_filter": "setsar=1",
        "esrgan_scale": 2,
    }
    payload.update(overrides)
    workroot.mkdir(parents=True, exist_ok=True)
    (workroot / "resume_manifest.json").write_text(
        _json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return workroot


def test_resume_reuses_verified_segments(monkeypatch, tmp_path):
    """U-B1: a matching manifest + verified segments -> the loop reuses
    them ('resumed chunk' steps) and _run_process is never called for
    extraction/upscale/segment on those chunks; the concat still runs."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=4)
    _stub_manifest_probe_ok(monkeypatch)

    events = []

    result = run_dvd_upscale(
        _resume_plan(tmp_path),
        on_progress=lambda frac, eta, detail=None: events.append(
            (frac, detail)),
    )
    assert result == (str(tmp_path / "out.mkv"), 8)
    # Zero extraction/upscale/segment subprocess work happened.
    work_labels = [label for label, _ in calls
                   if not label.startswith("upscale mezzanine concat")]
    assert work_labels == []
    # Every chunk reported 'resumed chunk' with an advancing frac.
    resumed = [d for _, d in events if d and d.get("step") == "resumed chunk"]
    assert len(resumed) == 4
    assert [d["chunk"] for d in resumed] == [1, 2, 3, 4]
    # The finalize announcement still fires (concat step, clamped frac).
    assert any(
        d and d.get("step") == FINALIZE_STEP_CONCAT for _, d in events)
    # The output mezzanine was rebuilt via concat.
    assert (tmp_path / "out.mkv").read_bytes() == b"mezzanine"


def test_resume_recomputes_on_count_mismatch(monkeypatch, tmp_path):
    """U-B2: one segment probes the wrong decode count -> exactly that
    chunk recomputes; siblings are still reused."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=4)

    def fake_run(cmd, **kw):
        # chunk_0002's segment probes 3 frames instead of 2.
        if "chunk_0002" in " ".join(cmd):
            return _fake_probe_result(frames=3, rate="24000/1001")
        return _fake_probe_result(frames=2, rate="24000/1001")

    monkeypatch.setattr(upscale_mod.subprocess, "run", fake_run)

    run_dvd_upscale(_resume_plan(tmp_path))
    extract_idx = [i for i, (label, _) in enumerate(calls)
                   if label.startswith("upscale frame extraction")]
    assert len(extract_idx) == 1, "exactly one chunk must recompute"
    # The recomputed chunk is chunk 3 (index 2): between(n,4,5).
    vf = calls[extract_idx[0]][1]
    assert "between(n,4,5)" in vf[vf.index("-vf") + 1]


def test_resume_recomputes_on_fps_mismatch(monkeypatch, tmp_path):
    """U-B3: a segment stamped at the wrong clock (30000/1001 vs plan
    24000/1001) recomputes - the count gate alone is blind to the
    v0.2.34 clock-lie class."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=4)

    def fake_run(cmd, **kw):
        if "chunk_0001" in " ".join(cmd):
            return _fake_probe_result(frames=2, rate="30000/1001")
        return _fake_probe_result(frames=2, rate="24000/1001")

    monkeypatch.setattr(upscale_mod.subprocess, "run", fake_run)

    run_dvd_upscale(_resume_plan(tmp_path))
    extract_idx = [i for i, (label, _) in enumerate(calls)
                   if label.startswith("upscale frame extraction")]
    assert len(extract_idx) == 1
    vf = calls[extract_idx[0]][1]
    assert "between(n,2,3)" in vf[vf.index("-vf") + 1]


def test_resume_fingerprint_mismatch_clears_workroot(monkeypatch, tmp_path):
    """U-B4: a manifest from a different plan intent (mode 720p_anime)
    -> the whole workroot is rmtree'd and every chunk recomputes; the
    debug line records the fingerprint decision."""
    calls = _resume_env(monkeypatch, tmp_path)
    # Manifest for the anime mode; the plan runs live-action.
    workroot = _manifest_for(tmp_path, mode="720p_anime")
    _seed_segments(workroot, count=4)
    _stub_manifest_probe_ok(monkeypatch)
    marker = workroot / "old_garbage.txt"
    marker.write_text("stale from another intent")

    debug_lines = []
    monkeypatch.setattr(
        upscale_mod, "_debug_line",
        lambda workroot, text: debug_lines.append(text))

    run_dvd_upscale(_resume_plan(tmp_path))
    # The old workroot contents are gone (wholesale clear), including
    # the marker - not silently reused.
    assert not marker.exists()
    # All 4 chunks were extracted (full recompute).
    extract = [label for label, _ in calls
               if label.startswith("upscale frame extraction")]
    assert len(extract) == 4
    # The debug lane records the fingerprint decision (§3.7).
    assert any("fingerprint=mismatch" in line for line in debug_lines)


def test_resume_corrupt_manifest_fails_closed(monkeypatch, tmp_path):
    """U-B5: a truncated/corrupt manifest is a mismatch -> wholesale
    recompute, no crash."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = tmp_path / "work"
    workroot.mkdir(parents=True, exist_ok=True)
    (workroot / "resume_manifest.json").write_text(
        '{"version": 1, "mode": "720p", "fps": "24000/1')  # truncated
    _seed_segments(workroot, count=4)
    _stub_manifest_probe_ok(monkeypatch)

    result = run_dvd_upscale(_resume_plan(tmp_path))
    assert result == (str(tmp_path / "out.mkv"), 8)
    extract = [label for label, _ in calls
               if label.startswith("upscale frame extraction")]
    assert len(extract) == 4


def test_resume_decode_failure_recomputes(monkeypatch, tmp_path):
    """U-B6: the segment probe itself fails (ffprobe nonzero exit) ->
    that chunk recomputes; a healthy sibling still resumes."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=4)

    def fake_run(cmd, **kw):
        if "chunk_0003" in " ".join(cmd):
            class _Bad:
                returncode = 1
                stdout = ""
                stderr = "ffprobe exploded"
            return _Bad()
        return _fake_probe_result(frames=2, rate="24000/1001")

    monkeypatch.setattr(upscale_mod.subprocess, "run", fake_run)

    run_dvd_upscale(_resume_plan(tmp_path))
    extract = [label for label, _ in calls
               if label.startswith("upscale frame extraction")]
    assert len(extract) == 1
    vf = calls[
        [i for i, (l, _) in enumerate(calls)
         if l.startswith("upscale frame extraction")][0]][1]
    assert "between(n,6,7)" in vf[vf.index("-vf") + 1]


def test_interrupted_mid_chunk_recomputes_that_chunk(monkeypatch, tmp_path):
    """U-B7: an interruption mid-chunk leaves partial out/ PNGs but no
    segment.mkv - the PNGs are NEVER resume units (§3.2): the chunk
    recomputes from extract, and the partial out/ is cleared."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=3)  # chunks 0-2 complete
    # Chunk 3 was interrupted after upscaled PNGs, before segment mux.
    partial_out = workroot / "chunk_0003" / "out"
    partial_out.mkdir(parents=True, exist_ok=True)
    (partial_out / "frame_00000007.png").write_bytes(png_header(1280, 720))
    (partial_out / "frame_00000008.png").write_bytes(png_header(1280, 720))
    _stub_manifest_probe_ok(monkeypatch)

    seen_segments = []
    _orig_run = upscale_mod._run_process

    def spy_run(cmd, *, label, cancel=None):
        # Capture the segment payload at mux time - the success path
        # rmtree's the workroot afterwards (U-B8), so post-run reads
        # would see nothing. The spy wraps the ORIGINAL fake (which
        # writes the payload), reading the file AFTER it ran.
        _orig_run(cmd, label=label, cancel=cancel)
        if label.startswith("upscale mezzanine segment"):
            seg_path = Path(cmd[-1])
            seen_segments.append((seg_path.parent.name,
                                   seg_path.read_bytes()))

    monkeypatch.setattr(upscale_mod, "_run_process", spy_run)

    run_dvd_upscale(_resume_plan(tmp_path))
    extract = [label for label, _ in calls
               if label.startswith("upscale frame extraction")]
    assert len(extract) == 1  # only chunk 4 recomputed
    # The recomputed chunk muxed a FRESH segment (its partial out/ PNGs
    # were never resume material - §3.2).
    assert ("chunk_0003", b"segment-payload") in seen_segments


def test_success_still_removes_workroot(monkeypatch, tmp_path):
    """U-B8: a completed run rmtree's the workroot even when chunks were
    resumed - the resume value is spent once the mezzanine is built and
    gated (regression pin on §3.5/§7.1)."""
    _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=4)
    _stub_manifest_probe_ok(monkeypatch)

    run_dvd_upscale(_resume_plan(tmp_path))
    assert not workroot.exists()


def test_output_mezzanine_unlinked_even_on_full_resume(monkeypatch, tmp_path):
    """U-B9: a stale output mezzanine from an interrupted concat is
    replaced - concat is redone every attempt (§3.5); resume semantics
    end at the segment layer."""
    calls = _resume_env(monkeypatch, tmp_path)
    workroot = _manifest_for(tmp_path)
    _seed_segments(workroot, count=4)
    _stub_manifest_probe_ok(monkeypatch)
    stale = tmp_path / "out.mkv"
    stale.write_bytes(b"truncated stale mezzanine from the killed run")

    run_dvd_upscale(_resume_plan(tmp_path))
    concat_calls = [c for c in calls
                    if c[0].startswith("upscale mezzanine concat")]
    assert len(concat_calls) == 1
    assert stale.read_bytes() == b"mezzanine"


def test_verify_segment_size_and_token_rules(tmp_path, monkeypatch):
    """Direct pins on _verify_segment (§3.4): zero-byte file refuses
    even before probing; a wrong-count probe refuses; a wrong-rate
    probe refuses; a probe crash refuses; the happy path accepts."""
    from bluray_to_mp4.upscale import _verify_segment

    def probe(good):
        def fake_run(cmd, **kw):
            if good:
                return _fake_probe_result(frames=2, rate="24000/1001")
            class _Bad:
                returncode = 1
                stdout = ""
                stderr = "x"
            return _Bad()
        monkeypatch.setattr(upscale_mod.subprocess, "run", fake_run)

    seg = tmp_path / "segment.mkv"
    seg.write_bytes(b"data")
    probe(True)
    assert _verify_segment(seg, 2, "24000/1001", ffmpeg_probe="ffprobe")
    # Expected count mismatch.
    assert not _verify_segment(
        seg, 3, "24000/1001", ffmpeg_probe="ffprobe")
    # Wrong rate token.
    probe(False)
    assert not _verify_segment(
        seg, 2, "24000/1001", ffmpeg_probe="ffprobe")
    # Zero-byte file (interrupted mux edge) never verifies.
    seg.write_bytes(b"")
    probe(True)
    assert not _verify_segment(
        seg, 2, "24000/1001", ffmpeg_probe="ffprobe")
    # Missing file.
    assert not _verify_segment(
        tmp_path / "nope.mkv", 2, "24000/1001", ffmpeg_probe="ffprobe")


def test_manifest_payload_ignores_attempt_started_at(tmp_path):
    """§3.3 pin: the informational attempt_started_at field is excluded
    from fingerprint comparison - a new attempt matches an old manifest
    even though its timestamp differs."""
    from bluray_to_mp4.upscale import (
        _RESUME_MANIFEST_ATTEMPT_KEY,
        _resume_manifest_matches,
        _resume_manifest_payload,
    )

    plan = UpscalePlan(
        input_path=str(tmp_path / "input.mkv"),
        output_path=str(tmp_path / "out.mkv"),
        workdir=str(tmp_path / "work"),
        mode="720p",
        duration_seconds=1,
        total_frames=8,
        fps="24000/1001",
    )
    payload = _resume_manifest_payload(
        plan, binary="/bin/r", model="realesrgan-x2plus",
        esrgan_scale=2, segment_filter="setsar=1",
        total_frames=8, chunk_frames=2,
    )
    workroot = tmp_path / "work"
    workroot.mkdir(parents=True)
    (workroot / "resume_manifest.json").write_text(
        _json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Same payload but a different attempt timestamp -> still a match.
    payload[_RESUME_MANIFEST_ATTEMPT_KEY] = "1999-01-01T00:00:00"
    assert _resume_manifest_matches(workroot, payload)
    # A single meaningful field off (fps) -> mismatch.
    payload["fps"] = "25"
    assert not _resume_manifest_matches(workroot, payload)
