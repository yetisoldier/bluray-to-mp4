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

def test_unlabeled_sub_still_recommended():
    """v0.2.14: a subtitle with no English confirmation is still selected
    (best non-commentary/descriptive track) with an explicit caveat in the
    reasons - the Review page must never show nothing (validation run 7:
    Mamma Mia's PGS tracks scored below the old >=100 threshold)."""
    from bluray_to_mp4.models import Attribute, StreamInfo, Title

    t = Title(tid=0)
    t.streams.append(StreamInfo(0, 0, {1: Attribute(1, 6201, "Video")}))
    t.streams.append(StreamInfo(0, 1, {1: Attribute(1, 6202, "Audio"),
                                       28: Attribute(28, 0, "eng"),
                                       29: Attribute(29, 0, "English")}))
    t.streams.append(StreamInfo(0, 2, {1: Attribute(1, 6203, "Subtitle"),
                                       5: Attribute(5, 0, "PGS")}))
    sel = recommend_title(t, want_subtitles=True)
    assert sel.subtitle_sid == 2
    assert any("not confirmed" in r for r in sel.subtitle_reasons)


def test_selection_subtitle_user_declined_default():
    """v0.2.15: the new Selection field defaults False so the
    recommendation engine's behavior (and every existing caller) is
    untouched; the Review page sets it only on an explicit
    "(no subtitles)" pick, and the pipeline uses it to gate the
    rip-time first-stream fallback."""
    from bluray_to_mp4.recommend import Selection

    disc = _movie_disc()
    sel = recommend_title(disc.title(0))
    assert sel.subtitle_user_declined is False
    assert Selection(tid=0).subtitle_user_declined is False
