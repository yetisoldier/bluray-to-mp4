"""Tests for the encode command builder and bitrate budget math."""

from bluray_to_mp4.encode import EncodePlan, build_encode_args, estimate_video_bitrate_kbps


def test_bitrate_budget_bluray_2gb_per_hour():
    # 2 GB/h = 4444 kbit/s total; minus slack and 448k AAC -> ~3863k video.
    assert estimate_video_bitrate_kbps() == 3863
    assert estimate_video_bitrate_kbps(audio_channels=2) == 4119


def test_bitrate_budget_formula_15gb_per_hour():
    # Pure formula pin (X GB/h * 2222 kbit/s total, minus slack and
    # 448k AAC); the DVD DEFAULT moved to 1.0 GB/h in v0.2.16.
    assert estimate_video_bitrate_kbps(target_gb_per_hour=1.5) == 2785


def test_bitrate_budget_dvd_default_10gb_per_hour():
    # v0.2.16 (Eric decision): DVDs now target 1.0 GB/hour.
    assert estimate_video_bitrate_kbps(target_gb_per_hour=1.0) == 1707


def test_nvenc_plan_bluray():
    plan = EncodePlan(
        input_path="in.mkv",
        output_path="out.mp4",
        duration_seconds=7590,
        media_type="bluray",
        encoder="nvenc",
        audio_sid=0,
    )
    args = build_encode_args(plan)
    joined = " ".join(args)
    assert "hevc_nvenc" in args
    assert "-b:v" in args
    assert f"{plan.target_video_kbps}k" == "3863k"
    assert args[args.index("-b:v") + 1] == "3863k"
    assert "scale=-2:1080" in joined
    assert "-movflags" in args and "+faststart" in args[args.index("-movflags") + 1]
    assert args[-1] == "out.mp4" and args[args.index("-f") + 1] == "mp4"
    assert "aac" in args


def test_x265_plan_dvd_keeps_sd_resolution():
    plan = EncodePlan(
        input_path="in.mkv",
        output_path="out.mp4",
        duration_seconds=2880,
        media_type="dvd",
        encoder="x265",
    )
    args = build_encode_args(plan)
    joined = " ".join(args)
    assert "libx265" in args
    # No upscaling for DVD sources.
    assert "scale" not in joined
    assert plan.gb_per_hour == 1.0
    assert plan.target_video_kbps == 1707


def test_two_input_plan_maps_video_from_mezzanine_audio_from_original():
    plan = EncodePlan(
        input_path="original.mkv",
        video_input_path="upscaled.mkv",
        output_path="out.mp4",
        duration_seconds=5400,
        media_type="dvd",
        encoder="nvenc",
        audio_sid=1,
        target_gb_per_hour=1.5,
    )
    args = build_encode_args(plan)
    assert args[args.index("-i") + 1] == "upscaled.mkv"
    assert args[args.index("-i", args.index("-i") + 1) + 1] == "original.mkv"
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == [
        "1:a:1?", "0:v:0"]


def test_deinterlace_single_vf():
    plan = EncodePlan(
        input_path="in.mkv",
        output_path="out.mp4",
        duration_seconds=5400,
        media_type="bluray",
        encoder="nvenc",
        deinterlace=True,
    )
    args = build_encode_args(plan)
    assert args.count("-vf") == 1
    vf = args[args.index("-vf") + 1]
    assert vf == "yadif,scale=-2:1080:flags=lanczos"

    sd = EncodePlan(
        input_path="in.mkv",
        output_path="out.mp4",
        duration_seconds=5400,
        media_type="dvd",
        encoder="x265",
        deinterlace=True,
    )
    sd_args = build_encode_args(sd)
    assert sd_args[sd_args.index("-vf") + 1] == "yadif"