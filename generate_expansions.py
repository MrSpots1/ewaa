import json
import re
import time
from pathlib import Path
from datetime import timedelta

import requests

# -------------------- CONFIG --------------------

INPUT_FILE = Path("filtered-english-words.txt")
OUTPUT_FILE = Path("word_expansions.json")
CHECKPOINT_FILE = Path("word_expansions_checkpoint.jsonl")
ERROR_LOG = Path("expansion_errors.log")
FAILED_FILE = Path("failed_words.txt")

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"

MAX_RETRIES = 3          # Retries after the initial attempt
REQUEST_TIMEOUT = 180
SAVE_EVERY = 1           # Append checkpoint after every completed word


SYSTEM_PROMPT = """
You generate acronym expansions for English words.

Given a target word, produce an expansion with EXACTLY one word
for each letter of the target word, in the same order.

HARD REQUIREMENTS:
1. The expansion must contain exactly len(word) words.
2. Each expansion word must start with the corresponding letter
   of the target word, ignoring capitalization.
3. The expansion must describe, define, or closely represent the
   target word's meaning.
4. Do not use the target word anywhere in the expansion, even as
   a substring of another word. Matching is case-insensitive.
5. Every expansion item must be a single English word.
6. Do not include punctuation, explanations, or extra fields.
7. The expansion should work together as a phrase or sentence that describes the target word. Not as individual synonyms. 

For example, for "cat", a structurally valid expansion might be:
{"word":"cat","expansion":["Curious","Animal","Tracker"]}

For "the", you MUST output three words beginning with T, H, E.
Do not output a normal dictionary definition with fewer words.

Return only a valid JSON object in this exact format:
{"word":"TARGET","expansion":["WORD1","WORD2"]}

The example format above is illustrative. The number of expansion
words must always equal the number of letters in TARGET.
"""


# -------------------- HELPERS --------------------

def log_error(word, attempt, message, raw=""):
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        f.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"word={word!r} attempt={attempt}: {message}\n"
        )
        if raw:
            f.write(f"Raw response: {raw[:2000]}\n")
        f.write("\n")


def clean_json_text(text):
    """Remove markdown fences and isolate the first JSON object."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Response did not contain a JSON object")
    return text[start:end + 1]


def clean_expansion_word(value):
    """Keep letters only, removing punctuation, digits, and unusual symbols."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^A-Za-z]", "", value).lower()



def validate_result(raw_text, target):
    json_text = clean_json_text(raw_text)
    result = json.loads(json_text)

    if not isinstance(result, dict):
        raise ValueError("JSON result must be an object")

    result_word = result.get("word")
    if not isinstance(result_word, str) or result_word.lower() != target.lower():
        raise ValueError(f"Wrong word field: {result_word!r}")

    expansion = result.get("expansion")
    if not isinstance(expansion, list):
        raise ValueError("'expansion' must be a JSON array")

    if len(expansion) != len(target):
        raise ValueError(
            f"Expected {len(target)} expansion words, got {len(expansion)}"
        )

    cleaned = []
    target_lower = target.lower()

    for index, (item, letter) in enumerate(zip(expansion, target_lower)):
        word = clean_expansion_word(item)

        if not word:
            raise ValueError(
                f"Expansion item {index + 1} is empty after cleaning"
            )

        if word[0].lower() != letter:
            raise ValueError(
                f"Item {index + 1} must start with {letter!r}; got {item!r}"
            )

        candidate = word.lower()

        # For short words, only reject exact matches.
        # For words of 3+ letters, reject any substring match.
        if (
            (len(target_lower) >= 3 and target_lower in candidate)
            or (len(target_lower) < 3 and target_lower == candidate)
        ):
            raise ValueError(
                f"Target word {target!r} appears inside expansion word {word!r}"
            )

        cleaned.append(word)

    return {"word": target, "expansion": cleaned}


def ask_ollama(target, previous_error=None):
    user_prompt = f"Create an acronym expansion for this exact target word: {target}"
    if previous_error:
        user_prompt += (
            "\nYour previous response failed validation for this reason: "
            + previous_error
            + "\nTry again and obey every rule exactly."
        )

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": 0.4},
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    return body["message"]["content"]


def load_completed():
    completed = {}
    if not CHECKPOINT_FILE.exists():
        return completed

    with CHECKPOINT_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                if isinstance(item, dict) and "word" in item and "expansion" in item:
                    completed[item["word"].lower()] = item
            except json.JSONDecodeError:
                continue
    return completed


def format_duration(seconds):
    return str(timedelta(seconds=max(0, int(seconds))))


def write_final_json(completed, original_words):
    # Preserve the original word-list order.
    ordered = [
        completed[word.lower()]
        for word in original_words
        if word.lower() in completed
    ]
    temp_file = OUTPUT_FILE.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp_file.replace(OUTPUT_FILE)


# -------------------- MAIN --------------------

def main():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Cannot find {INPUT_FILE}. Put it in this script's folder."
        )

    try:
        requests.get("http://localhost:11434/api/tags", timeout=5).raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(
            "Cannot reach Ollama at localhost:11434. Start Ollama and try again."
        ) from exc

    with INPUT_FILE.open("r", encoding="utf-8") as f:
        words = list(dict.fromkeys(
            line.strip() for line in f
            if line.strip() and re.fullmatch(r"[A-Za-z]+", line.strip())
        ))
        words = words[:10]

    completed = load_completed()
    todo = [word for word in words if word.lower() not in completed]
    total = len(words)
    started = time.time()
    succeeded_this_run = 0
    failed_this_run = 0

    print(f"Model: {MODEL}")
    print(f"Words in input: {total:,}")
    print(f"Already completed: {len(words) - len(todo):,}")
    print(f"Remaining: {len(todo):,}")
    print(f"Maximum attempts per word: {MAX_RETRIES + 1}")
    print("Starting...\n")

    for index, target in enumerate(todo, start=1):
        word_started = time.time()
        last_error = None
        success = None

        for attempt in range(1, MAX_RETRIES + 2):
            try:
                raw = ask_ollama(target, last_error)
                success = validate_result(raw, target)
                break
            except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                log_error(target, attempt, last_error, locals().get("raw", ""))
                print(f"\n[ERROR] {target!r}, attempt {attempt}/{MAX_RETRIES + 1}: {last_error}")

        if success is not None:
            completed[target.lower()] = success
            with CHECKPOINT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(success, ensure_ascii=False) + "\n")
            succeeded_this_run += 1
        else:
            failed_this_run += 1
            with FAILED_FILE.open("a", encoding="utf-8") as f:
                f.write(target + "\n")

        processed = index
        elapsed = time.time() - started
        average = elapsed / processed
        eta = average * (len(todo) - processed)
        overall_done = total - (len(todo) - processed)
        percent = (overall_done / total * 100) if total else 100

        print(
            f"\rProgress: {overall_done:,}/{total:,} ({percent:.2f}%)"
            f" | This run: {processed:,}/{len(todo):,}"
            f" | Success: {succeeded_this_run:,}"
            f" | Failed: {failed_this_run:,}"
            f" | Elapsed: {format_duration(elapsed)}"
            f" | ETA: {format_duration(eta)}",
            end="",
            flush=True,
        )

    print("\n\nWriting final JSON file...")
    write_final_json(completed, words)

    print(f"Finished in {format_duration(time.time() - started)}")
    print(f"Successful this run: {succeeded_this_run:,}")
    print(f"Failed this run: {failed_this_run:,}")
    print(f"JSON output: {OUTPUT_FILE.resolve()}")
    print(f"Checkpoint: {CHECKPOINT_FILE.resolve()}")
    print(f"Error log: {ERROR_LOG.resolve()}")
    print(f"Failed words: {FAILED_FILE.resolve()}")


if __name__ == "__main__":
    main()
