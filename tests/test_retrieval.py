from rag.chunker import chunk_source
def test_chunk_preserves_lines():
    chunks=chunk_source("a\\nb\\nc","x.txt",max_lines=2,overlap=0)
    assert chunks and chunks[0]["start_line"]==1
