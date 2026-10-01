"""Tests for the recommendation engine (best audio/subtitle picks)."""

from bluray_to_mp4.makemkv import scan_disc
from bluray_to_mp4.recommend import recommend_disc, recommend_title
from tests.fixtures import MOVIE_DISC_OUTPUT, fake_runner


def _movie_disc():
    return scan_disc(0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))


def test_picks_best_english_audio_and_skips_commentary():
    disc = _movie_disc()
    title = disc.title(0)
    sel = recommend_title(title)
    # DTS-HD Master Audio 7.1 (sid 1), not the AC3 commentary (sid 2)
    assert sel.audio_sid == 1
    assert sel.audio_sid != 2
    assert sel.has_english_audio
    assert any("7.1" in r or "8 channels" in r for r in sel.audio_reasons)


def test_picks_normal_english_subs_over_sdh_and_foreign():
    disc = _movie_disc()
    title = disc.title(0)
    sel = recommend_title(title)
    # sid 4 = plain "English"; sid 3 = "English (SDH)"; sid 5 = Spanish
    assert sel.subtitle_sid == 4
    assert sel.has_english_subs


def test_recommend_disc_covers_all_titles():
    disc = _movie_disc()
    sels = recommend_disc(disc.titles)
    assert len(sels) == len(disc.titles)
    assert all(s.tid == t.tid for s, t in zip(sels, disc.titles))


def test_no_english_audio_warns():
    disc = _movie_disc()
    title = disc.title(0)
    # Wipe language codes: force "foreign audio only" scenario.
    for stream in title.streams:
        for attr_id in (28, 29):
            if attr_id in stream.attrs:
                stream.attrs[attr_id].value = ""
    sel = recommend_title(title)
    assert sel.audio_sid is not None
    assert any("No English audio" in w for w in sel.warnings)


def test_subtitle_toggle():
    disc = _movie_disc()
    title = disc.title(0)
    sel = recommend_title(title, want_subtitles=False)
    assert sel.subtitle_sid is None
    assert sel.subtitle_reasons == []