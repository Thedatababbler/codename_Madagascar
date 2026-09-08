import json

from orchestra.control.fast_loop.llm_diagnosis import _utf8_clean


def test_lone_surrogates_are_replaced():
    bad = "tail: " + b"\xff\xfe".decode("utf-8", "surrogateescape")
    msgs = _utf8_clean([{"role": "user", "content": bad}, {"role": "system", "content": "ok"}])
    text = json.dumps(msgs)
    assert "\\udc" not in text and msgs[1]["content"] == "ok"
    msgs[0]["content"].encode("utf-8")  # now encodable
