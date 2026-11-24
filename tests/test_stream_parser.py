"""Regression: with web search the RESPONSE fragment arrives inside a BATCH patch.

Run: .venv\\Scripts\\python tests\\test_stream_parser.py
"""

from chat_deepseek_api.stream_parser import StreamParser

STREAM = "\n".join([
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"fragments":'
    '[{"id":2,"type":"SEARCH","content":null,"results":[]}]}}}',
    'data: {"p":"response/fragments/-1/content","o":"SET","v":"Found 5 pages."}',
    'data: {"p":"response/fragments/-1/results","o":"APPEND","v":[{"url":"https://x"}]}',
    'data: {"p":"response","o":"BATCH","v":[{"p":"fragments","v":['
    '{"id":2,"type":"SEARCH","content":"Found 17 pages."},'
    '{"id":4,"type":"RESPONSE","content":"Iron"}]},{"p":"has_pending_fragment","v":false}]}',
    'data: {"p":"response/fragments/-1/content","o":"APPEND","v":" Ing"}',
    'data: {"v":"ot"}',
    'data: {"p":"response","o":"BATCH","v":[{"p":"quasi_status","v":"FINISHED"}]}',
    "",
])


def test_batch_fragments_with_search():
    parser = StreamParser()
    pieces = list(parser.feed(STREAM)) + list(parser.flush())
    assert "".join(p.choices[0].delta.content for p in pieces) == "Iron Ingot"
    assert {p.choices[0].delta.type for p in pieces} == {"text"}
    assert parser.finished


if __name__ == "__main__":
    test_batch_fragments_with_search()
    print("ok")
