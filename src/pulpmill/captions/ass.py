"""Advanced SubStation Alpha subtitle generation.

ASS rather than SRT because burned-in short-form captions need per-word colour,
a heavy outline and precise placement, none of which SRT can express. ffmpeg
renders ASS natively through libass, so this costs no extra dependency.

**Word highlighting is one event per word.** ASS has a native karaoke mode
(`\\k`), but it colours every *already-spoken* word, which is the wrong look:
short-form captions highlight only the word being said. Emitting one Dialogue
event per word -- each showing the whole cue with a single word recoloured --
produces exactly that, and stays readable in the generated file.

**Scraped text is escaped, not trusted.** `{`, `}` and `\\` are ASS override
syntax. A story body containing `{\\an8}` would otherwise reposition the
captions, and something more creative could do worse. `escape_text` strips that
capability rather than trying to detect misuse.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pulpmill.config.models import CaptionConfig, TitleCardConfig
from pulpmill.domain.media import CaptionCue

#: ASS override syntax, neutralised in any text taken from a source.
_ESCAPES = str.maketrans({"{": "(", "}": ")", "\\": "/", "\n": " ", "\r": " "})


def escape_text(text: str) -> str:
    """Make scraped text safe to place inside a Dialogue line."""
    return text.translate(_ESCAPES).strip()


def _escape_title(text: str) -> str:
    r"""Escape a title while preserving deliberate `\N` line breaks.

    The compositor inserts `\N` between a title and its part label. Escaping
    that away would run them together, so the break is protected across the
    translation and restored afterwards.
    """
    placeholder = "\x00LINEBREAK\x00"
    protected = text.replace("\\N", placeholder)
    return escape_text(protected).replace(placeholder, "\\N")


def format_timestamp(seconds: float) -> str:
    """ASS timestamps are `H:MM:SS.cc` -- centiseconds, single-digit hours."""
    if seconds < 0:
        seconds = 0.0
    centiseconds = round(seconds * 100)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6000)
    secs, cents = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def build_style_block(config: CaptionConfig, *, width: int, height: int) -> str:
    """The `[V4+ Styles]` section.

    Alignment 2 is bottom-centre, so `MarginV` is measured up from the bottom
    edge -- which is why `vertical_position` (measured from the top) is
    inverted here.
    """
    margin_v = max(0, round((1.0 - config.vertical_position) * height))
    margin_h = max(0, round(config.horizontal_margin * width))
    bold = -1 if config.bold else 0
    fields = [
        "Caption",
        config.font_family,
        str(config.font_size),
        config.primary_colour,
        config.highlight_colour,
        config.outline_colour,
        "&H64000000",  # back colour: soft shadow, 39% alpha
        str(bold),
        "0",  # italic
        "0",  # underline
        "0",  # strikeout
        "100",  # scale x
        "100",  # scale y
        "0",  # spacing
        "0",  # angle
        "1",  # border style: outline + shadow
        f"{config.outline_width:g}",
        f"{config.shadow_depth:g}",
        "2",  # alignment: bottom centre
        str(margin_h),
        str(margin_h),
        str(margin_v),
        "1",  # encoding: default
    ]
    return "Style: " + ",".join(fields)


def build_title_style(config: TitleCardConfig, caption: CaptionConfig) -> str:
    """A second style for the opening title card.

    Alignment 5 is middle-centre, so the title sits above the caption band
    rather than colliding with it. Wrapping is left on for this style alone:
    a title is one long phrase and does need to break.
    """
    fields = [
        "Title",
        caption.font_family,
        str(config.font_size),
        caption.primary_colour,
        caption.primary_colour,
        caption.outline_colour,
        "&H96000000",
        "-1",
        "0",
        "0",
        "0",
        "100",
        "100",
        "0",
        "0",
        "1",
        f"{caption.outline_width:g}",
        f"{caption.shadow_depth:g}",
        "5",
        "120",
        "120",
        "0",
        "1",
    ]
    return "Style: " + ",".join(fields)


def build_ass(
    cues: Sequence[CaptionCue],
    config: CaptionConfig,
    *,
    width: int,
    height: int,
    title: str = "pulpmill captions",
    title_card: TitleCardConfig | None = None,
    title_text: str = "",
) -> str:
    """Render a complete ASS document."""
    header = [
        "[Script Info]",
        f"Title: {escape_text(title)}",
        "ScriptType: v4.00+",
        # WrapStyle 0 is balanced smart wrapping. Cues are sized to fit on one
        # line, so this normally does nothing -- but when a long word or a wide
        # font would overflow, wrapping is a far better failure than WrapStyle 2,
        # which silently clips the text off both edges of the frame.
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        build_style_block(config, width=width, height=height),
    ]
    if title_card is not None and title_card.enabled:
        header.append(build_title_style(title_card, config))
    header += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    if title_card is not None and title_card.enabled and title_text.strip():
        clipped = title_text.strip()[: title_card.max_chars]
        # Layer 1 so the title draws above the caption band if they overlap.
        # \pos overrides the style's alignment anchor so the card sits where
        # `vertical_position` asks, rather than dead centre.
        anchor_x = width // 2
        anchor_y = round(title_card.vertical_position * height)
        events.append(
            f"Dialogue: 1,{format_timestamp(0.0)},{format_timestamp(title_card.seconds)},"
            f"Title,,0,0,0,,{{\\pos({anchor_x},{anchor_y})\\fad(220,320)}}"
            f"{_escape_title(clipped)}"
        )
    events.extend(_build_events(cues, config))
    return "\n".join([*header, *events, ""])


def _build_events(cues: Sequence[CaptionCue], config: CaptionConfig) -> list[str]:
    events: list[str] = []
    for cue in cues:
        if config.karaoke and cue.words:
            events.extend(_karaoke_events(cue, config))
        else:
            events.append(_dialogue(cue.start_seconds, cue.end_seconds, escape_text(cue.text)))
    return events


def _karaoke_events(cue: CaptionCue, config: CaptionConfig) -> list[str]:
    """One event per word, each recolouring exactly that word."""
    words = [escape_text(timing.word) for timing in cue.words]
    events: list[str] = []
    for index, timing in enumerate(cue.words):
        start = timing.start_seconds if index else cue.start_seconds
        end = timing.end_seconds if index < len(cue.words) - 1 else cue.end_seconds
        if end <= start:
            continue
        rendered = " ".join(
            f"{{\\c{config.highlight_colour}}}{word}{{\\c{config.primary_colour}}}"
            if position == index
            else word
            for position, word in enumerate(words)
        )
        events.append(_dialogue(start, end, rendered))
    return events


def _dialogue(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{format_timestamp(start)},{format_timestamp(end)},Caption,,0,0,0,,{text}"


def write_ass(
    path: Path,
    cues: Sequence[CaptionCue],
    config: CaptionConfig,
    *,
    width: int,
    height: int,
    title: str = "pulpmill captions",
    title_card: TitleCardConfig | None = None,
    title_text: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_ass(
            cues,
            config,
            width=width,
            height=height,
            title=title,
            title_card=title_card,
            title_text=title_text,
        ),
        encoding="utf-8",
    )
    return path
