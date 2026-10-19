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
