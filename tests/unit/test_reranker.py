from app.services.reranker import BGEReranker


class FakeCrossEncoder:
    def predict(self, pairs, **kwargs):
        assert kwargs["show_progress_bar"] is False
        return [0.1 if "wrong" in text else 0.9 for _, text in pairs]


def test_bge_reranker_returns_original_candidates_in_score_order():
    candidates = [
        {"text": "wrong chunk", "source": "wrong.md"},
        {"text": "relevant chunk", "source": "right.md"},
    ]
    reranker = BGEReranker(model=FakeCrossEncoder())

    result = reranker.rerank("question", candidates, top_n=1)

    assert result == [candidates[1]]
