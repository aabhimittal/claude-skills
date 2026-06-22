# Safe patterns for production LLM code

Reference for the fixes the analyzer recommends. Anthropic SDK syntax shown;
the principles apply to any provider.

## 1. Keys come from the environment, never source

```python
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
# or simply: anthropic.Anthropic()  — the SDK reads ANTHROPIC_API_KEY itself
```

If a key was ever committed, treat it as compromised and rotate it.

## 2. Keep the system prompt static; isolate untrusted input

The system prompt carries operator authority and is the prompt-cache prefix.
Interpolating user input into it enables prompt injection and busts caching.

```python
SYSTEM = "You are a concise, accurate support agent."   # byte-stable
client.messages.create(
    model="claude-opus-4-8", max_tokens=1024, system=SYSTEM,
    messages=[{"role": "user", "content": user_question}],   # untrusted text here
)
```

Need to inject *trusted* runtime context mid-conversation (a mode switch, the
user's timezone)? On Opus 4.8, append a `{"role": "system"}` message to
`messages[]` — it keeps the cached prefix intact and is the non-spoofable
operator channel:

```python
messages=[*history, {"role": "user", "content": ...},
          {"role": "system", "content": "User timezone is America/New_York."}]
```

## 3. Use current model IDs

Current generation: `claude-opus-4-8` (most capable), `claude-sonnet-4-6`
(balanced), `claude-haiku-4-5` (fast). Use the bare alias — do not append a
date snapshot to an alias. Retired IDs (`claude-2.x`, `claude-3-*`,
`claude-instant`) return 404.

## 4. Adaptive thinking, not budget_tokens

Fixed `thinking.budget_tokens` is removed on Fable 5 / Opus 4.8 / 4.7 (400) and
deprecated elsewhere:

```python
client.messages.create(
    model="claude-opus-4-8", max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},   # low | medium | high | max
    messages=[...],
)
```

Sampling parameters (`temperature`, `top_p`, `top_k`) are also removed on those
models (400) — delete them and steer with prompting.

## 5. Set max_tokens; stream large outputs

`max_tokens` is required on the Anthropic Messages API. For large outputs
(> ~16K), stream so the request doesn't exceed the SDK's HTTP timeout:

```python
with client.messages.stream(
    model="claude-opus-4-8", max_tokens=64000, messages=[...],
) as stream:
    msg = stream.get_final_message()
```

## 6. Let the SDK handle retries and timeouts

The official SDKs auto-retry `429`/`408`/`409`/`5xx` with exponential backoff
(default `max_retries=2`) and default to a 10-minute timeout. Tune via the
client (`Anthropic(max_retries=5, timeout=30.0)`) rather than hand-rolling a
retry loop.

## 7. Structured output via output_config, not output_format

```python
client.messages.create(
    model="claude-opus-4-8", max_tokens=1024,
    output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    messages=[...],
)
# or the typed helper: client.messages.parse(..., output_format=MyPydanticModel)
```
