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
            (in_dir / "frame_00000001.png").write_bytes(png_header(640, 360))
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "frame_00000001.png").write_bytes(png_header(1280, 720))
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
        )
    )

    assert result == str(output)
    extract_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale frame extraction")
    )
    assert extract_cmd[extract_cmd.index("-vf") + 1] == (
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
            (in_dir / "frame_00000001.png").write_bytes(png_header(640, 360))
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "frame_00000001.png").write_bytes(png_header(1280, 720))
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
            source_width=720,
            source_height=480,
            display_aspect_ratio="16:9",
            sample_aspect_ratio="32:27",
        )
    )

    extract_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale frame extraction")
    )
    assert extract_cmd[extract_cmd.index("-vf") + 1] == (
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
            (in_dir / "frame_00000001.png").write_bytes(png_header(640, 360))
        elif label.startswith("Real-ESRGAN"):
            seen.append(cmd)
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "frame_00000001.png").write_bytes(png_header(1280, 720))
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
            (in_dir / "frame_00000001.png").write_bytes(png_header(640, 360))
        elif label.startswith("Real-ESRGAN"):
            seen.append(cmd)
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "frame_00000001.png").write_bytes(png_header(1280, 720))
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
            (in_dir / "frame_00000001.png").write_bytes(png_header(640, 360))
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "frame_00000001.png").write_bytes(png_header(1280, 720))
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


def _recording_run_process(calls):
    def fake_run(cmd, *, label, cancel=None):
        calls.append((label, cmd))
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            (in_dir / "frame_00000001.png").write_bytes(png_header(640, 360))
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "frame_00000001.png").write_bytes(png_header(1280, 720))
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
    )
    assert plan.fps == "30000/1001"
    run_dvd_upscale(plan)
    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    assert segment_cmd[segment_cmd.index("-framerate") + 1] == "30000/1001"


def test_duration_gate_rejects_mismatched_mezzanine():
    """A mezzanine 20% shorter than the rip (the run-13 drift shape)
    is refused with an UpscaleError naming BOTH durations - the
    pipeline then encodes the original rip with an honest warning."""
    from bluray_to_mp4.upscale import verify_mezzanine_duration

    def probe(path, *, binary="ffprobe"):
        return 6072.0 if "mezz" in path else 7590.0

    with pytest.raises(UpscaleError, match=r"6072\.0s.*7590\.0s|does not match"):
        verify_mezzanine_duration("mezz.mkv", 7590.0, probe_duration=probe)


def test_duration_gate_accepts_matched_mezzanine():
    """A mezzanine within max(5s, 2%) of the rip passes the gate - VFR
    audio padding and container rounding must never trip it."""
    from bluray_to_mp4.upscale import verify_mezzanine_duration

    verify_mezzanine_duration(
        "mezz.mkv", 7590.0, probe_duration=lambda p, *, binary="ffprobe": 7592.0
    )
    # Short rip: the 5-second floor governs (2% of 60s is only 1.2s).
    verify_mezzanine_duration(
        "mezz.mkv", 60.0, probe_duration=lambda p, *, binary="ffprobe": 64.5
    )


def test_duration_gate_floor_boundary_rejects_beyond_5s():
    from bluray_to_mp4.upscale import verify_mezzanine_duration

    with pytest.raises(UpscaleError, match="does not match"):
        verify_mezzanine_duration(
            "mezz.mkv", 60.0, probe_duration=lambda p, *, binary="ffprobe": 65.5
        )


def test_duration_gate_rejects_unprobeable_mezzanine():
    """A mezzanine ffprobe cannot read cannot be trusted - refuse it
    (fail open to the original rip), never encode a blind guess."""
    from bluray_to_mp4.upscale import verify_mezzanine_duration

    def broken(path, *, binary="ffprobe"):
        raise RuntimeError("ffprobe failed")

    with pytest.raises(UpscaleError, match="could not be probed"):
        verify_mezzanine_duration("mezz.mkv", 7590.0, probe_duration=broken)
