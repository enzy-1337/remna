from shared.services.media_search.pinterest import extract_pin_id, is_pinterest_url


def test_is_pinterest_url_pin_page():
    assert is_pinterest_url("https://www.pinterest.com/pin/664281013778109217/")
    assert is_pinterest_url("https://pin.it/abc123")


def test_is_pinterest_url_other_platforms():
    assert not is_pinterest_url("https://www.tiktok.com/@user/video/1")
    assert not is_pinterest_url("https://www.pinterest.com/user/board/")


def test_extract_pin_id():
    assert extract_pin_id("https://www.pinterest.com/pin/664281013778109217/") == "664281013778109217"
    assert (
        extract_pin_id(
            "https://pinterest.com/pin/dive-into-serenity--2885187256207927"
        )
        == "2885187256207927"
    )
