from pathlib import Path

import pytest

from bluray_to_mp4 import upscale as upscale_mod
from bluray_to_mp4.upscale import UpscalePlan, run_dvd_upscale


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def test_upscale_uses_display_aspect_filters_and_dynamic_tile(monkeypatch, tmp_path):
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
    assert esrgan_cmd[esrgan_cmd.index("-t") + 1] == "704"

    segment_cmd = next(
        cmd for label, cmd in calls if label.startswith("upscale mezzanine segment")
    )
    assert segment_cmd[segment_cmd.index("-vf") + 1] == (
        "scale=w='trunc(720*dar/2)*2':h=720:flags=lanczos,setsar=1"
    )


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

    assert seen[0][seen[0].index("-t") + 1] == expected
