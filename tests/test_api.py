import pdbr


def test_api_attr():
    expected = ["run", "set_trace"]
    assert [dir for dir in dir(pdbr) if not dir.startswith("__")] == expected
