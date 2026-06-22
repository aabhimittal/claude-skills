"""The same intent as unsafe_app.py, written safely.
analyze_llm_code.py reports no findings for this file.
"""
import os
import anthropic

# Key comes from the environment / secrets manager, never source.
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Static system prompt — byte-stable, so prompt caching can hit, and untrusted
# text never gains operator authority.
SUPPORT_SYSTEM = "You are a concise, accurate customer-support agent."


def answer(user_question: str) -> str:
    response = client.messages.create(
        model="claude-opus-4-8",                 # current model
        max_tokens=1024,
        system=SUPPORT_SYSTEM,
        # Untrusted input stays in the user turn.
        messages=[{"role": "user", "content": user_question}],
    )
    return response.content[0].text


def summarize(doc: str) -> str:
    # Large output -> stream, and use adaptive thinking instead of budget_tokens.
    with client.messages.stream(
        model="claude-opus-4-8",
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": f"Summarize:\n{doc}"}],
    ) as stream:
        return stream.get_final_message().content[0].text
