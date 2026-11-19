"""Validator tests for the consumer manifest `object_storage` block (spec consumer-object-storage, R1/AC-4)."""

from __future__ import annotations

import pytest

import consumer_manifest as cm


def _m(block, slug="dronefleet"):
    return {"slug": slug, "object_storage": block}


def test_object_storage_is_an_enumerated_service_key():
    assert "object_storage" in cm.CONSUMABLE_SERVICE_KEYS


def test_minimal_block_is_accepted():
    assert cm.validate_manifest(_m({"buckets": ["artifacts", "datasets"]}))


def test_manifest_without_the_block_is_unchanged():
    assert cm.validate_manifest({"slug": "dronefleet"}) == {"slug": "dronefleet"}


@pytest.mark.parametrize(
    "block, field",
    [
        ("artifacts", "object_storage"),
        ({}, "object_storage.buckets"),
        ({"buckets": []}, "object_storage.buckets"),
        ({"buckets": "artifacts"}, "object_storage.buckets"),
        ({"buckets": ["artifacts", "artifacts"]}, "object_storage.buckets"),
        ({"buckets": ["flight-logs"]}, "object_storage.buckets[0]"),
        ({"buckets": ["Logs"]}, "object_storage.buckets[0]"),
        ({"buckets": [""]}, "object_storage.buckets[0]"),
        ({"buckets": [7]}, "object_storage.buckets[0]"),
        ({"buckets": ["a" * 33]}, "object_storage.buckets[0]"),
        ({"buckets": ["ok"], "website": "ok"}, "object_storage.website"),
        ({"buckets": ["ok"], "website": ["other"]}, "object_storage.website[0]"),
        ({"buckets": ["ok"], "website": ["ok", "ok"]}, "object_storage.website"),
        ({"buckets": ["ok"], "acl": "public"}, "object_storage.acl"),
    ],
)
def test_invalid_blocks_are_rejected_naming_the_field(block, field):
    with pytest.raises(cm.ManifestValidationError) as exc:
        cm.validate_manifest(_m(block))
    assert field in str(exc.value)


def test_bucket_name_length_is_checked_with_the_slug():
    slug = "a" * 20
    purpose = "p" * 32  # proj- (5) + 20 + - (1) + 32 = 58 <= 63: accepted
    assert cm.validate_manifest(_m({"buckets": [purpose]}, slug=slug))
    # The longest legal slug and purpose always fit, so the limit is a guard against future regex changes.
    assert len(f"proj-{slug}-{purpose}") <= 63


def test_hyphen_free_purposes_make_bucket_names_unambiguous():
    """Defect fixed by this spec: slug `a` + `b-x` and slug `a-b` + `x` must not both be expressible."""
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest(_m({"buckets": ["b-x"]}, slug="a"))
    assert cm.validate_manifest(_m({"buckets": ["x"]}, slug="a-b"))


def test_website_subset_is_accepted():
    assert cm.validate_manifest(_m({"buckets": ["artifacts", "docs"], "website": ["docs"]}))
    assert cm.validate_manifest(_m({"buckets": ["artifacts"], "website": []}))
