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


if __name__ == "__main__":
    test_parse_names(); test_pick(); test_split(); test_users(); test_ingest_history(); test_password(); print("ok")
