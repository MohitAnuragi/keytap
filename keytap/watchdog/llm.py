"""
Local model (Ollama) for the nightly run. It only labels and summarizes; keytap alone decides pass or
fail, and nothing the model says is executed. Answers are forced into a JSON schema, and a triage
that quotes screen text the phone never showed is demoted to "unknown".
"""

import json
import urllib.error
import urllib.request

URL = "http://localhost:11434/api/chat"
NUM_CTX = 8192   # Ollama's default of 4096 silently cuts a long prompt; an ANR screen alone can be 12k tokens
MAX_STR = 80
CATEGORIES = ["popup_blocking", "wrong_screen", "selector_changed", "slow_loading",
              "app_regression_suspected", "unknown"]

TRIAGE_SYSTEM = """You triage one failed step of an Android UI test. You get a JSON failure report.
Pick the category:
- popup_blocking: a dialog, permission prompt or banner covers the screen (its buttons are in clickable)
- wrong_screen: the app is open but on a different screen than the step expects
- selector_changed: the target exists under a different id, text or description (a similar one is in clickable)
- slow_loading: the screen still shows a loading state or is partly built
- app_regression_suspected: the expected element is gone and nothing on screen replaces it
- unknown: none of these clearly fits
summary: one sentence, at most 25 words.
evidence: up to 3 strings copied exactly from the report's ids, texts, text, content_desc or resource_id."""

CRASH_SYSTEM = "You read an Android crash stack trace. Reply with one sentence, at most 25 words: the exception, where it happens, and the likely cause."

TRIAGE_SCHEMA = {"type": "object", "required": ["category", "summary", "evidence"],
                 "properties": {"category": {"enum": CATEGORIES}, "summary": {"type": "string"},
                                "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 3}}}
CRASH_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}


def clip(value):
    """Long strings cut to MAX_STR, recursively. A stack trace in one content_desc must not eat the prompt."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR - 3] + "..."
    if isinstance(value, list):
        return [clip(v) for v in value]
    if isinstance(value, dict):
        return {k: clip(v) for k, v in value.items()}
    return value


def _post(body, timeout):
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def chat(model, system, user, schema, timeout=300):
    """(parsed JSON answer, raw response). The raw response carries Ollama's timing fields.
    The schema goes into the prompt and into Ollama's `format` field; some models answer 501
    "structured output is unavailable", then it is asked again without `format`. Either way the
    answer is checked against the schema's required keys."""
    body = {"model": model, "stream": False, "keep_alive": "30m", "think": False, "options": {"temperature": 0, "num_ctx": NUM_CTX},
            "messages": [{"role": "system", "content": f"{system}\nReply with JSON only, matching this schema: {json.dumps(schema)}"},
                         {"role": "user", "content": user}]}
    for attempt in (1, 2):   # without `format` a model sometimes renames a key; ask once more
        try:
            raw = _post({**body, "format": schema}, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 501:
                raise
            raw = _post(body, timeout)
        if raw.get("prompt_eval_count", 0) >= NUM_CTX - 64:
            raise ValueError(f"prompt filled the {NUM_CTX} token context window, the answer cannot be trusted")
        text = raw["message"]["content"]
        try:
            answer, _ = json.JSONDecoder().raw_decode(text[text.find("{"):])   # skips code fences, ignores trailing text
            missing = [k for k in schema["required"] if k not in answer]
        except ValueError:
            answer, missing = {}, schema["required"]
        if not missing:
            return answer, raw
        body["messages"] += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"Invalid: the JSON must have exactly the keys {schema['required']}. Reply again."}]
    raise ValueError(f"model answer lacks {missing}: {text[:200]}")


def compact(failure):
    """The part of failure.json the model needs, small enough for a short prompt."""
    s = failure.get("screen", {})
    return clip({"cause": failure.get("cause"), "step": failure.get("action"), "error": failure.get("error"),
                 "expected_app": failure.get("package"), "foreground": failure.get("foreground"),
                 "ids": s.get("ids", [])[:20], "texts": s.get("texts", [])[:12],
                 "clickable": s.get("clickable", [])[:30]})


def triage(model, failure):
    """{"category", "summary", "evidence"} for one failure.json content."""
    report = compact(failure)
    answer, raw = chat(model, TRIAGE_SYSTEM, json.dumps(report), TRIAGE_SCHEMA)
    if answer["category"] not in CATEGORIES:
        answer["category"] = "unknown"
    answer["evidence"] = [e for e in answer["evidence"] if isinstance(e, str)]
    seen = json.dumps(report)
    if any(json.dumps(e)[1:-1] not in seen for e in answer["evidence"]):   # seen is JSON text, so a newline in it is the two characters \n
        answer = {**answer, "category": "unknown", "summary": answer["summary"] + " (evidence not on screen, demoted)"}
    return answer, raw


def summarize_crash(model, excerpt):
    answer, raw = chat(model, CRASH_SYSTEM, excerpt, CRASH_SCHEMA)
    return answer["summary"], raw
