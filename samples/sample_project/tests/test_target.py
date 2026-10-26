from target import normalize_tags


def test_trims_lowercases_and_dedupes():
    assert normalize_tags([" Python ", "PYTHON", "FastAPI"]) == ["fastapi", "python"]


def test_drops_blank_entries_and_sorts():
    assert normalize_tags(["b", "", "  ", "a", "b"]) == ["a", "b"]


def test_accepts_an_empty_list():
    assert normalize_tags([]) == []