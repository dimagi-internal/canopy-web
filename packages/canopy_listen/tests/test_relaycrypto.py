"""The phone and the laptop must agree byte for byte, and the relay must be able to do
nothing but carry frames. relay_vectors.json is what frontend/src/listen/relayCrypto.ts is
tested against too."""
import json
from pathlib import Path

import pytest

rc = pytest.importorskip("canopy_listen.relaycrypto")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

V = json.loads((Path(__file__).parent / "relay_vectors.json").read_text())


def key(d):
    return ec.derive_private_key(int.from_bytes(rc.unb64u(d), "big"), ec.SECP256R1())


def test_both_ends_derive_the_pinned_keys():
    sid = V["session_id"]
    laptop, static, eph = key(V["laptop"]["d"]), key(V["phone_static"]["d"]), key(V["phone_eph"]["d"])
    want = (rc.unb64u(V["up"]), rc.unb64u(V["down"]))
    assert rc.session_keys_laptop(laptop, V["phone_eph"]["pub"], V["phone_static"]["pub"], sid) == want
    assert rc.session_keys_phone(eph, static, V["laptop"]["pub"], sid) == want
    assert rc.pair_keys(rc.unb64u(V["pairing_secret"]), sid) == (rc.unb64u(V["pair_up"]), rc.unb64u(V["pair_down"]))


def test_the_pinned_frames_open_and_say_what_they_should():
    sid = V["session_id"]
    up = rc.Opener(rc.unb64u(V["up"]), sid)
    kind, body = up.open(rc.unb64u(V["up_frames"][0]["frame_b64u"]))
    assert kind == rc.AUDIO and body == rc.unb64u(V["up_frames"][0]["body_b64u"])
    kind, body = up.open(rc.unb64u(V["up_frames"][1]["frame_b64u"]))
    assert kind == rc.CONTROL and json.loads(body) == {"type": "end"}
    down = rc.Opener(rc.unb64u(V["down"]), sid)
    assert json.loads(down.open(rc.unb64u(V["down_frames"][0]["frame_b64u"]))[1])["type"] == "ready"
    pair = rc.Opener(rc.unb64u(V["pair_up"]), sid)
    assert json.loads(pair.open(rc.unb64u(V["pair_frames"][0]["frame_b64u"]))[1])["phone_key"] == V["phone_static"]["pub"]


def test_the_relay_can_not_replay_reorder_forge_or_cross_sessions():
    sid = V["session_id"]
    frames = [rc.unb64u(f["frame_b64u"]) for f in V["up_frames"]]
    up = rc.Opener(rc.unb64u(V["up"]), sid)
    up.open(frames[0])
    with pytest.raises(rc.BadFrame, match="replay"):
        up.open(frames[0])
    up.open(frames[1])
    tampered = bytearray(frames[1])
    tampered[-1] ^= 1
    with pytest.raises(rc.BadFrame):
        rc.Opener(rc.unb64u(V["up"]), sid).open(bytes(tampered))
    with pytest.raises(rc.BadFrame, match="forged"):
        rc.Opener(rc.unb64u(V["up"]), "another-session").open(frames[0])
    with pytest.raises(rc.BadFrame, match="forged"):  # the down key can't open up frames
        rc.Opener(rc.unb64u(V["down"]), sid).open(frames[0])


def test_a_relay_that_swaps_in_its_own_phone_key_gets_different_keys():
    sid = V["session_id"]
    laptop = key(V["laptop"]["d"])
    impostor = rc.new_private()
    real = rc.session_keys_laptop(laptop, V["phone_eph"]["pub"], V["phone_static"]["pub"], sid)
    fake = rc.session_keys_laptop(laptop, V["phone_eph"]["pub"], rc.public_b64(impostor), sid)
    assert real != fake
