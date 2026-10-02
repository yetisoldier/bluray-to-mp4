"""Round-trip tests for MakeMKV robot-mode parsing against fixture output."""

import pytest

from bluray_to_mp4.makemkv import (
    BetaKeyExpiredError,
    DiscOpenError,
    MakeMkvError,
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

def test_rip_titles_ignores_disc_internal_file_added_lines():
    """MakeMKV 2.0.0 also emits 3307 lines for disc-internal files (the
    00803.mpls playlist) with a bare filename; only real .mkv outputs
    are saved files (validation run 5: rip hit 100%, then ffprobe died
    on 'No such file or directory' because saved[0] was the playlist)."""
    script = [
        'MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV v2.0.0"',
        'MSG:3307,0,2,"File 00803.mpls was added","File %1 was added","00803.mpls","1"',
        'MSG:3307,0,2,"File was added as title #0","File %1 was added as title #%2","/tmp/out/movie_t00.mkv","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed","%1 titles saved, %2 failed","1","0"',
    ]
    saved = rip_titles(0, [0], "/tmp/out", runner=fake_runner(script))
    assert saved == ["/tmp/out/movie_t00.mkv"]


def test_rip_titles_recovers_saved_files_from_outdir():
    """MakeMKV 2.0.0 no longer reports finished .mkv files via MSG:3307
    (those lines now carry disc-internal files like the .mpls playlist);
    validation run 6: a 100% rip raised 'finished without producing any
    files' while the .mkv sat in outdir. After a successful 'N titles
    saved', the paths must be recovered from the output directory."""
    import os
    out = "/tmp/b2m_2000_out"
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "Mamma Mia! Here We Go Again_t00.mkv"), "wb") as f:
        f.write(b"ripped")
    script = [
        'MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV v2.0.0"',
        'MSG:3307,0,2,"File 00803.mpls was added","File %1 was added","00803.mpls","1"',
        'MSG:5004,0,2,"1 titles saved, 0 failed","%1 titles saved, %2 failed","1","0"',
    ]
    saved = rip_titles(0, [0], out, runner=fake_runner(script))
    assert len(saved) == 1
    assert saved[0].endswith("Mamma Mia! Here We Go Again_t00.mkv")
    assert not any(p.endswith(".mpls") for p in saved)
    import shutil
    shutil.rmtree(out, ignore_errors=True)


def test_rip_titles_recovers_mkvs_without_any_summary(tmp_path):
    """v0.2.16 (validation run 9, 14:52): the DVD rip emitted NO MSG:3307
    .mkv lines and NO MSG:5004 summary at all - a 100% rip raised
    'finished without producing any files' while the .mkv sat in outdir.
    With no saved-file entries and no title failures, .mkv files in
    outdir are this run's product (the pipeline purges the dir before
    ripping) and must be recovered unconditionally."""
    out = tmp_path / "dvd_out"
    out.mkdir()
    mkv = out / "Mamma Mia! Here We Go Again_t00.mkv"
    mkv.write_bytes(b"ripped")
    script = [
        'MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV v2.0.0"',
        'MSG:3307,0,2,"File 00803.mpls was added","File %1 added","00803.mpls","1"',
        "PRGV:65536,65536,65536",
    ]
    saved = rip_titles(0, [0], str(out), runner=fake_runner(script))
    assert saved == [str(mkv)]
    assert not any(p.endswith(".mpls") for p in saved)


def test_rip_titles_no_summary_empty_outdir_still_raises(tmp_path):
    """Guard: no MSG:3307 .mkv entries, no summary line, and an EMPTY
    outdir is still a genuine nothing-was-produced failure - the
    existing error fires (recovery must not invent files)."""
    out = tmp_path / "empty_out"
    out.mkdir()
    script = [
        'MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV v2.0.0"',
        "PRGV:65536,65536,65536",
    ]
    with pytest.raises(MakeMkvError, match="without producing any files"):
        rip_titles(0, [0], str(out), runner=fake_runner(script))
