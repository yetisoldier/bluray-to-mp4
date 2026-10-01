"""Round-trip tests for MakeMKV robot-mode parsing against fixture output."""

import pytest

from bluray_to_mp4.makemkv import (
    BetaKeyExpiredError,
    DiscOpenError,
    find_drives,
    parse_line,
    rip_titles,
    scan_disc,
)
from tests.fixtures import (
    BETA_EXPIRED_OUTPUT,
    DRIVE_SCAN_OUTPUT,
    MOVIE_DISC_OUTPUT,
    SERIES_DISC_OUTPUT,
    fake_runner,
)


def test_parse_drv_line():
    kind, payload = parse_line(
        'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 1.02","BLADE2049","D:"'
    )
    assert kind == "DRV"
    idx, status, dtype, name, label, path = payload
    assert (idx, status, dtype) == (0, 2, 12)
    assert label == "BLADE2049"
    assert path == "D:"


def test_parse_prgv():
    kind, payload = parse_line("PRGV:32768,65536,65536")
    assert kind == "PRGV"
    assert payload == (32768, 65536, 65536)


def test_find_drives_filters_unattached():
    drives = find_drives(runner=fake_runner(DRIVE_SCAN_OUTPUT.splitlines()))
    assert len(drives) == 2  # the 256-status slot is excluded
    assert drives[0].has_disc and drives[0].disc_type == "bluray"
    assert drives[0].disc_label == "BLADE2049"
    assert not drives[1].has_disc


def test_scan_movie_disc():
    disc = scan_disc(0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))
    assert disc.name == "Blade Runner 2049"
    assert disc.media_type == "bluray"
    assert len(disc.titles) == 4

    main = disc.title(0)
    assert main is not None
    assert main.duration_seconds == 2 * 3600 + 6 * 60 + 30
    assert main.chapters == 19
    assert main.filename == "Blade_Runner_2049_t00.mkv"
    assert len(main.streams_of("video")) == 1
    assert len(main.streams_of("audio")) == 2
    assert len(main.streams_of("subtitle")) == 3

    dts = main.streams_of("audio")[0]
    assert dts.sid == 1
    assert dts.lang_code == "eng"
    assert dts.channels == 8
    assert dts.bitrate_kbps == 5156
    assert "Master Audio" in dts.codec_long


def test_scan_series_disc_durations():
    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    assert len(disc.titles) == 5
    episodes = [t for t in disc.titles if t.duration_seconds >= 600]
    assert [t.tid for t in episodes] == [0, 1, 2, 3]
    assert all(len(t.streams_of("audio")) >= 1 for t in episodes)


def test_beta_expired_raises_with_hint():
    with pytest.raises(BetaKeyExpiredError) as exc:
        scan_disc(0, runner=fake_runner(BETA_EXPIRED_OUTPUT.splitlines()))
    assert "forum" in str(exc.value)


def test_scan_disc_open_error():
    lines = [
        "DRV:0,2,999,12,\"BD-RE\",\"LABEL\",\"D:\"",
        'MSG:5010,0,0,"Failed to open disc","Failed to open disc"',
    ]
    with pytest.raises(DiscOpenError):
        scan_disc(0, runner=fake_runner(lines))


def test_rip_titles_reports_progress_and_saved_files():
    script = [
        'MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV v1.17.8"',
        "PRGV:16384,65536,65536",
        "PRGV:49152,65536,65536",
        'MSG:3307,0,2,"File was added as title #0","File %1 was added as title #%2","/tmp/out/movie_t00.mkv","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed","%1 titles saved, %2 failed","1","0"',
    ]
    progress = []
    saved = rip_titles(
        0, [0], "/tmp/out", runner=fake_runner(script),
        on_progress=lambda frac, prgv: progress.append(frac),
    )
    assert saved == ["/tmp/out/movie_t00.mkv"]
    assert progress[0] == pytest.approx(0.25)
    assert progress[-1] == pytest.approx(0.75)


def test_rip_titles_zero_saved_raises():
    script = [
        'MSG:5004,0,2,"0 titles saved, 0 failed","%1 titles saved, %2 failed","0","0"',
    ]
    with pytest.raises(Exception) as exc:
        rip_titles(0, [0], "/tmp/out", runner=fake_runner(script))
    assert "0 titles" in str(exc.value) or "without producing" in str(exc.value)