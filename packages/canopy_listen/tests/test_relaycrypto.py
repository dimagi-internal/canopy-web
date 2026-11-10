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


def test_pairing_by_code_matches_the_pinned_commitment_code_and_handshake():
    c, sid = V["code_pairing"], V["session_id"]
    np_, nl = rc.unb64u(c["phone_nonce"]), rc.unb64u(c["laptop_nonce"])
    S, L = V["phone_static"]["pub"], V["laptop"]["pub"]
    assert rc.pair_commit(S, np_) == c["commit"]
    assert rc.pair_code(sid, S, L, np_, nl) == c["code"]
    hello = rc.read_handshake(rc.unb64u(c["hello_frame_b64u"]))
    assert hello == {"type": "hello", "laptop_key": L, "nonce": c["laptop_nonce"], "name": "Mac"}


def test_a_different_key_session_or_nonce_changes_the_code():
    sid, np_, nl = V["session_id"], rc.unb64u(V["code_pairing"]["phone_nonce"]), rc.unb64u(V["code_pairing"]["laptop_nonce"])
    S, L, other = V["phone_static"]["pub"], V["laptop"]["pub"], V["phone_eph"]["pub"]
    code = rc.pair_code(sid, S, L, np_, nl)
    assert code != rc.pair_code(sid, S, other, np_, nl)
    assert code != rc.pair_code(sid + "x", S, L, np_, nl)
    assert code != rc.pair_code(sid, S, L, np_, bytes(32))
    for bad in (b"", b"\x01{}", b"\x02[1]", b"\x02not json"):
        with pytest.raises(rc.BadFrame):
            rc.read_handshake(bad)
