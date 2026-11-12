"""Tests for the final-output container seam (v0.2.38)."""

from bluray_to_mp4 import container


def test_container_constants_shape():
    """The seam concentrates every container fact; names end .mkv and
    the muxer/format pair matches the Hermes-verified matroska facts."""
    assert container.VIDEO_EXT == ".mkv"
    assert isinstance(container.VIDEO_EXT, str) and len(container.VIDEO_EXT) == 4
    assert container.FFMPEG_FORMAT == "matroska"
    assert container.TEXT_SUBTITLE_CODEC == "subrip"


def test_bitmap_copy_codecs_membership():
    """Exactly the two bitmap codecs a MakeMKV rip of a BD/DVD can
    carry; both names are the H.264-side codec names ffmpeg probes."""
    assert container.BITMAP_COPY_CODECS == frozenset(
        {"hdmv_pgs_subtitle", "dvd_subtitle"}
    )
    assert "hdmv_pgs_subtitle" in container.BITMAP_COPY_CODECS
    assert "dvd_subtitle" in container.BITMAP_COPY_CODECS
    assert "subrip" not in container.BITMAP_COPY_CODECS


def test_output_naming_derives_from_seam():
    """Naming derives from the seam: movie/episode names and sidecars
    never hard-code the retired .mp4 literal."""
    from bluray_to_mp4.naming import movie_output_name, subtitle_sidecar
    from bluray_to_mp4.series import episode_output_names

    assert movie_output_name("Up") == f"Up{container.VIDEO_EXT}"
    names = episode_output_names("Show", 1, episode_tids=[0])
    assert names[0][1].endswith(container.VIDEO_EXT)
    assert subtitle_sidecar(f"X{container.VIDEO_EXT}") == "X.srt"
