"""Terminal presentation helpers.

The only module allowed to know what the output looks like. Commands compute
results and hand them here, which keeps `--json` output and human output from
drifting apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pulpmill.domain.ranking import RankedStory
from pulpmill.normalization.text import truncate

console = Console()
error_console = Console(stderr=True)


def score_colour(score: float) -> str:
    if score >= 70:
        return "bright_green"
    if score >= 50:
        return "green"
    if score >= 30:
        return "yellow"
    return "red"


def ok(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/yellow] {message}")


def fail(message: str) -> None:
    error_console.print(f"[red]✗[/red] {message}")


def heading(text: str) -> None:
    console.print()
    console.print(f"[bold]{text}[/bold]")


def key_values(pairs: Mapping[str, Any], *, title: str | None = None) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="dim")
    table.add_column()
    for key, value in pairs.items():
        table.add_row(key, str(value))
    if title:
        console.print(Panel(table, title=title, title_align="left", expand=False))
    else:
        console.print(table)


def counts_table(title: str, counts: Mapping[str, int], *, total_label: str = "total") -> None:
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("key")
    table.add_column("count", justify="right")
    for key, value in counts.items():
        table.add_row(key, f"{value:,}")
    if counts:
        table.add_section()
        table.add_row(f"[dim]{total_label}[/dim]", f"[bold]{sum(counts.values()):,}[/bold]")
    console.print(table)


def candidates_table(
    candidates: Sequence[RankedStory],
    *,
    title: str = "Top candidates",
    show_url: bool = True,
) -> None:
    """Render ranked stories, highest first."""
    table = Table(title=title, title_justify="left", header_style="bold", expand=True)
    table.add_column("#", justify="right", width=3, style="dim")
    table.add_column("score", justify="right", width=6)
    table.add_column("source", width=10)
    table.add_column("community", width=18, overflow="ellipsis")
    table.add_column("title", overflow="ellipsis", ratio=2)
    table.add_column("words", justify="right", width=6)
    if show_url:
        table.add_column("url", overflow="ellipsis", ratio=1, style="dim")

    for position, entry in enumerate(candidates, start=1):
        story = entry.story
        community = str(story.metadata.get("quality_key") or "-")
        row: list[str | Text] = [
            str(position),
            Text(
                f"{entry.ranking.final_score:.1f}",
                style=score_colour(entry.ranking.final_score),
            ),
            story.source_platform,
            community,
            truncate(story.title, 90),
            f"{story.word_count:,}",
        ]
        if show_url:
            row.append(story.canonical_url)
        table.add_row(*row)

    console.print(table)


def signal_table(component_scores: Mapping[str, float], explanation: Mapping[str, Any]) -> None:
    """Show why a story scored what it did, signal by signal."""
    signals = explanation.get("signals", {})
    table = Table(title="Ranking breakdown", title_justify="left", header_style="bold")
    table.add_column("signal")
    table.add_column("value", justify="right")
    table.add_column("weight", justify="right")
    table.add_column("contribution", justify="right")
    table.add_column("notes", overflow="fold")

    for name, value in sorted(component_scores.items(), key=lambda item: -item[1]):
        detail = signals.get(name, {}) if isinstance(signals, Mapping) else {}
        available = detail.get("available", True)
        weight = detail.get("effective_weight", 0.0)
        contribution = detail.get("contribution", 0.0)
        notes = ""
        if not available:
            inner = detail.get("detail", {})
            reason = inner.get("reason") if isinstance(inner, Mapping) else None
            notes = f"[dim]unavailable: {reason or 'not reported by this source'}[/dim]"
        table.add_row(
            name,
            f"{value:.3f}",
            f"{float(weight):.3f}",
            f"{float(contribution):.2f}",
            notes,
        )
    console.print(table)


def story_panel(story: Any, *, excerpt_chars: int = 800) -> None:
    """Full provenance and content excerpt for one story."""
    key_values(
        {
            "id": story.id,
            "status": story.status.value,
            "source": story.source_platform,
            "source id": story.source_id,
            "url": story.canonical_url,
            "author": story.author or "-",
            "created": story.created_at.isoformat(),
            "discovered": story.discovered_at.isoformat(),
            "updated": story.updated_at.isoformat(),
            "words": f"{story.word_count:,}",
            "content hash": story.content_hash[:16],
            "duplicate of": story.duplicate_of_id or "-",
        },
        title=truncate(story.title, 100),
    )
    console.print(
        Panel(
            truncate(story.normalized_content, excerpt_chars),
            title="content excerpt",
            title_align="left",
        )
    )
