"""Example AI integration with several production hazards.
Run: analyze_llm_code.py unsafe_app.py
"""
import anthropic

# SEC001: hard-coded API key (and forces rotation via a code change)
client = anthropic.Anthropic(api_key="sk-ant-api03-EXAMPLEEXAMPLEEXAMPLE1234567890")


def answer(user_question: str) -> str:
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=64000,        # REL002: large max_tokens on a non-streaming call
        temperature=0.7,         # API002: sampling params 400 on this model
        # SEC002: untrusted input interpolated into the *system* prompt
        system=f"You are a support agent for {user_question}",
        messages=[{"role": "user", "content": user_question}],
    )
    return response.content[0].text


def summarize(doc: str) -> str:
    # REL001: missing max_tokens; API001: legacy thinking budget (400)
    return client.messages.create(
        model="claude-opus-4-8",
        thinking={"type": "enabled", "budget_tokens": 8000},
        messages=[{"role": "user", "content": f"Summarize:\n{doc}"}],
    )


def legacy_call(prompt: str) -> str:
    # MOD001: retired model -> 404
    return client.messages.create(
        model="claude-2.1",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
