from app.utils.text_splitter import RecursiveTextSplitter


def test_splitter_keeps_short_text_as_one_chunk():
    splitter = RecursiveTextSplitter(chunk_size=100, chunk_overlap=10)

    assert splitter.split_text("短文本") == ["短文本"]


def test_splitter_adds_overlap_between_chunks():
    splitter = RecursiveTextSplitter(chunk_size=8, chunk_overlap=2, separators=[" "])
    chunks = splitter.split_text("aa bb cc dd ee ff")

    assert len(chunks) > 1
    assert chunks[1].startswith(chunks[0][-2:])


def test_splitter_force_splits_long_text_without_separator():
    splitter = RecursiveTextSplitter(chunk_size=5, chunk_overlap=0, separators=[""])
    chunks = splitter.split_text("abcdefghijk")

    assert chunks == ["abcde", "fghij", "k"]
