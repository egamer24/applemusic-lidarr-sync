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


if __name__ == "__main__":
    test_parse_names(); test_pick(); test_split(); print("ok")
