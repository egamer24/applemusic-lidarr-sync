import app


def test_parse_names():
    assert app.parse_names(b"Drake\nSZA\n\ndrake\n", "text/plain") == ["Drake", "SZA"]
    assert app.parse_names(b'["A", "B", "a"]', "application/json") == ["A", "B"]
    assert app.parse_names(b'{"artists": ["X"]}', "") == ["X"]


def test_pick():
    results = [
        {"id": "1", "name": "Drake Bell", "score": 100},
        {"id": "2", "name": "Drake", "score": 95},
        {"id": "3", "name": "Beyonce", "score": 92, "aliases": [{"name": "Beyoncé"}]},
    ]
    assert app.pick("drake", results) == "2"
    assert app.pick("Beyoncé", results) == "3"
    assert app.pick("Nobody", results) is None
    assert app.pick("Ne-Yo", [{"id": "4", "name": "Ne\u2010Yo", "score": 100}]) == "4"
    assert app.pick("D'Angelo", [{"id": "5", "name": "D\u2019Angelo", "score": 100}]) == "5"
    assert app.pick("Ke$ha", [{"id": "6", "name": "Kesha", "score": 100}]) == "6"
    assert app.pick("Cee Lo Green", [{"id": "7", "name": "CeeLo Green", "score": 100}]) == "7"
    assert app.pick("Joe", [{"id": "8", "name": "Joe Cocker", "score": 100}, {"id": "9", "name": "Joe", "score": 80}]) == "9"


def test_split():
    assert app.SPLIT.split("Nipsey Hussle & Bino Rideaux") == ["Nipsey Hussle", "Bino Rideaux"]
    assert app.SPLIT.split("Drake feat. Rihanna") == ["Drake", "Rihanna"]


def test_users():
    app.state.update(users={"me": {"names": ["A", "B"], "last_ingest": None}, "wife": {"names": ["B", "C"], "last_ingest": None}},
                     cache={"A": ["1"], "B": ["2"], "C": ["2", "3"]})
    assert app.all_names() == ["A", "B", "C"]
    assert app.mbids() == ["1", "2", "3"]
    assert app.USER.match("wife") and not app.USER.match("../x")


def test_ingest_history(tmp="/tmp/amls-test"):
    import os
    os.makedirs(tmp, exist_ok=True)
    app.STATE_FILE = os.path.join(tmp, "state.json")
    app.state.update(users={}, cache={})
    app.ingest("me", ["A", "B"])
    r = app.ingest("me", ["B", "C"])
    assert r == {"user": "me", "received": 2, "added": 1, "removed": 1}
    h = app.state["users"]["me"]["history"][0]
    assert h["added"] == ["C"] and h["removed"] == ["A"]


def test_password():
    app.state["admin"] = {"user": "me", **app.hash_password("correct horse")}
    app.state["secret"] = "00" * 32
    assert app.check_password("Me", "correct horse")
    assert not app.check_password("me", "wrong") and not app.check_password("you", "correct horse")
    before = app.session_for(app.state["admin"])
    app.state["admin"].update(app.hash_password("new password!"))
    assert app.session_for(app.state["admin"]) != before


def test_totp():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key "12345678901234567890"
    assert app.totp_code(secret, 59 // 30) == "287082"            # RFC vector 94287082, 6 digits
    assert app.totp_code(secret, 1111111109 // 30) == "081804"    # RFC vector 07081804
    now = int(app.time.time() // 30)
    good = app.totp_code(secret, now)
    assert app.totp_step(secret, good) == now
    assert app.totp_step(secret, good, after=now) is None          # replay refused
    assert app.totp_step(secret, app.totp_code(secret, now - 3)) is None  # too old
    a = {"totp": secret, "totp_last": 0}
    codes = app.new_recovery_codes(a)
    assert app.check_second_factor(a, codes[0].upper()) and not app.check_second_factor(a, codes[0])  # single use
    assert app.check_second_factor(a, good) and not app.check_second_factor(a, good)
    assert not app.check_second_factor(a, "") and not app.check_second_factor(a, "000000x")


def test_cookies():
    app.state["secret"] = "11" * 32
    a = {"user": "me", "hash": "h"}
    s1 = app.session_for(a)
    a["totp"] = "ABC"
    assert app.session_for(a) != s1                                # enabling 2FA signs other browsers out
    assert app.pending_for(a, 100) != app.session_for(a).split(":")[1]


def test_lockout():
    app._fails.clear()
    for _ in range(9):
        app.record_failure()
    assert not app.locked_out()
    app.record_failure()
    assert app.locked_out()
    app._fails.clear()


if __name__ == "__main__":
    test_parse_names(); test_pick(); test_split(); test_users(); test_ingest_history(); test_password(); test_totp(); test_cookies(); test_lockout(); print("ok")
