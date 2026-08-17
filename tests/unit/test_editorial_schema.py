"""Validation of editorial provider output.

Model output is untrusted. Every one of these cases is something a model can
plausibly return, and every one must be rejected rather than acted on.
"""

from __future__ import annotations

import pytest

from pulpmill.domain.errors import EditorialResponseError
from pulpmill.editorial.schema import (
    SELECTION_JSON_SCHEMA,
    parse_json_payload,
    validate_selection,
)

ALLOWED = ["story-a", "story-b", "story-c"]


def payload(*items: dict[str, object], notes: str = "ok") -> dict[str, object]:
    return {"selections": list(items), "notes": notes}


def item(story_id: str, position: int) -> dict[str, object]:
    return {
        "story_id": story_id,
        "position": position,
        "rationale": "strong hook",
        "hook_strength": 4,
        "category": "relationships",
    }


class TestJsonParsing:
    def test_plain_json_parses(self) -> None:
        assert parse_json_payload('{"a": 1}') == {"a": 1}

    def test_a_fenced_block_parses(self) -> None:
        assert parse_json_payload('```json\n{"a": 1}\n```') == {"a": 1}

    @pytest.mark.parametrize("text", ["", "not json at all", "{unclosed", "[1, 2,]"])
    def test_malformed_json_raises_a_typed_error(self, text: str) -> None:
        with pytest.raises(EditorialResponseError, match="valid JSON"):
            parse_json_payload(text)


class TestSelectionValidation:
    def test_a_well_formed_response_is_accepted(self) -> None:
        result = validate_selection(
            payload(item("story-a", 1), item("story-b", 2)),
            allowed_ids=ALLOWED,
            expected_count=2,
        )
        assert [entry.story_id for entry in result.selections] == ["story-a", "story-b"]

    def test_an_invented_story_id_is_rejected(self) -> None:
        """The failure that would otherwise render a video for a nonexistent story."""
        with pytest.raises(EditorialResponseError, match="not a candidate"):
            validate_selection(
                payload(item("story-a", 1), item("hallucinated", 2)),
                allowed_ids=ALLOWED,
                expected_count=2,
            )

    def test_a_duplicated_story_is_rejected(self) -> None:
        with pytest.raises(EditorialResponseError, match="same story twice"):
            validate_selection(
                payload(item("story-a", 1), item("story-a", 2)),
                allowed_ids=ALLOWED,
                expected_count=2,
            )

    @pytest.mark.parametrize("count", [1, 3])
    def test_the_wrong_number_of_selections_is_rejected(self, count: int) -> None:
        with pytest.raises(EditorialResponseError, match="wrong number"):
            validate_selection(
                payload(item("story-a", 1), item("story-b", 2)),
                allowed_ids=ALLOWED,
                expected_count=count,
            )

    def test_duplicate_positions_are_rejected(self) -> None:
        with pytest.raises(EditorialResponseError, match="positions must be"):
            validate_selection(
                payload(item("story-a", 1), item("story-b", 1)),
                allowed_ids=ALLOWED,
                expected_count=2,
            )

    def test_gaps_in_positions_are_rejected(self) -> None:
        with pytest.raises(EditorialResponseError, match="positions must be"):
            validate_selection(
                payload(item("story-a", 1), item("story-b", 3)),
                allowed_ids=ALLOWED,
                expected_count=2,
            )

    def test_positions_must_start_at_one(self) -> None:
        with pytest.raises(EditorialResponseError):
            validate_selection(
                payload(item("story-a", 0), item("story-b", 1)),
                allowed_ids=ALLOWED,
                expected_count=2,
            )

    def test_a_non_object_payload_is_rejected(self) -> None:
        for bad in ([], "text", 42, None):
            with pytest.raises(EditorialResponseError, match="JSON object"):
                validate_selection(bad, allowed_ids=ALLOWED, expected_count=1)

    def test_a_missing_selections_key_is_rejected(self) -> None:
        with pytest.raises(EditorialResponseError, match="schema validation"):
            validate_selection({"notes": "hi"}, allowed_ids=ALLOWED, expected_count=1)

    def test_extra_keys_are_rejected(self) -> None:
        bad = payload(item("story-a", 1))
        bad["unexpected"] = True
        with pytest.raises(EditorialResponseError, match="schema validation"):
            validate_selection(bad, allowed_ids=ALLOWED, expected_count=1)

    def test_a_wrongly_typed_field_is_rejected(self) -> None:
        broken = item("story-a", 1)
        broken["position"] = "first"
        with pytest.raises(EditorialResponseError, match="schema validation"):
            validate_selection(payload(broken), allowed_ids=ALLOWED, expected_count=1)

    def test_an_out_of_range_hook_strength_is_rejected(self) -> None:
        broken = item("story-a", 1)
        broken["hook_strength"] = 99
        with pytest.raises(EditorialResponseError, match="schema validation"):
            validate_selection(payload(broken), allowed_ids=ALLOWED, expected_count=1)

    def test_out_of_order_input_is_accepted_and_ordered_by_position(self) -> None:
        result = validate_selection(
            payload(item("story-b", 2), item("story-a", 1)),
            allowed_ids=ALLOWED,
            expected_count=2,
        )
        ordered = sorted(result.selections, key=lambda entry: entry.position)
        assert [entry.story_id for entry in ordered] == ["story-a", "story-b"]


class TestJsonSchemaShape:
    def test_the_schema_forbids_extra_properties(self) -> None:
        assert SELECTION_JSON_SCHEMA["additionalProperties"] is False
        items = SELECTION_JSON_SCHEMA["properties"]["selections"]["items"]
        assert items["additionalProperties"] is False

    def test_every_property_is_required(self) -> None:
        """Structured outputs requires it, and it removes a whole class of gaps."""
        items = SELECTION_JSON_SCHEMA["properties"]["selections"]["items"]
        assert set(items["required"]) == set(items["properties"])

    def test_unsupported_constraint_keywords_are_absent(self) -> None:
        """Structured outputs rejects `minimum`/`maxLength`; ranges use enums."""
        rendered = repr(SELECTION_JSON_SCHEMA)
        for keyword in ("minimum", "maximum", "minLength", "maxLength", "multipleOf"):
            assert keyword not in rendered
