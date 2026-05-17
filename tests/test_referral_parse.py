from shared.services.pending_start_referral import should_store_pending_referral
from shared.services.referral_parse import parse_referral_code_from_start_args


def test_parse_referral_code() -> None:
    assert parse_referral_code_from_start_args("ref_ABC12") == "ABC12"
    assert parse_referral_code_from_start_args("REF_xyz") == "xyz"
    assert parse_referral_code_from_start_args("promo10") is None
    assert parse_referral_code_from_start_args(None) is None


def test_should_store_pending_referral() -> None:
    assert should_store_pending_referral("ref_TEST01") is True
    assert should_store_pending_referral("other") is False
    assert should_store_pending_referral(None) is False
