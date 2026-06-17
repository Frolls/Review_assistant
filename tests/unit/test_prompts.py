from app.prompts.review import build_review_messages


def test_review_prompt_roles_are_ordered_system_then_user():
    messages = build_review_messages("Как отревьюить PR с Ansible role?")

    assert [message.role for message in messages] == ["system", "user"]
    assert "senior ИИ-ассистент для ревью кода" in messages[0].content
    assert "Python Enhancement Proposals" in messages[0].content
    assert "Ansible community documentation" in messages[0].content
    assert "Как отревьюить PR с Ansible role?" in messages[1].content


def test_review_prompt_escapes_user_braces_before_template_interpolation():
    messages = build_review_messages("Покажи {malicious.format} и JSON {\"x\": 1}")

    user_content = messages[1].content

    assert "{{malicious.format}}" in user_content
    assert "{{\"x\": 1}}" in user_content
