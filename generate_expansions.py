import json
import os
import re
import select
import shutil
import sys
import termios
import time
import tty
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

import requests
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# -------------------- CONFIG --------------------

INPUT_FILE = Path("filtered-english-words.txt")
OUTPUT_FILE = Path("word_expansions.json")
CHECKPOINT_FILE = Path("word_expansions_checkpoint.jsonl")
ERROR_LOG = Path("expansion_errors.log")
FAILED_FILE = Path("failed_words.txt")
EXPANSION_WORDS_FILE = Path("expansion_words.txt")

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gemma4:31b-it-q4_K_M"
THINK = False            # Let the model reason before answering (slower, more accurate)

MAX_TRIES = 5            # Total attempts per word (the first try plus retries)
MAX_RETRIES = MAX_TRIES - 1
REQUEST_TIMEOUT = 300

JUDGE_ENABLED = True     # Set to False to skip judging and accept the first valid answer
JUDGE_PASS = 7           # Judge score needed to accept an answer right away
JUDGE_THINK = False      # Let the judge reason before scoring (slower)
JUDGE_TEMPERATURE = 0.0
SAVE_EVERY = 1           # Append checkpoint after every completed word

PACE_WINDOW = 50         # Recent words used for the rolling pace and ETA
RECENT_ROWS = 12         # Rows shown in the recent results table
REFRESH_PER_SECOND = 4

FORECAST_MIN_SAMPLES = 20  # Processed words needed before the snowball forecast shows
FORECAST_MAX_STEPS = 300_000

FILLER_WORDS = {
    "everywhere", "things", "thing", "stuff", "interesting", "important",
    "various", "specific", "influence", "influences", "overall", "general",
    "related", "unique",
}


SYSTEM_PROMPT = """
You write acronym expansions. Given a target word, you produce one entry for
each letter of the target. Each entry is an English word that starts with that
letter. The words, read in order, form a short, natural phrase that defines or
describes the target word, the way a dictionary gloss would.

Example of the goal:
  "cat" -> Curious Animal Tracker. Read together: a curious animal that hunts.
  Not acceptable: Cuddly Apple Tree (the words do not describe a cat).

HOW TO WORK
1. Write the target's letters with positions. Example: "contact" = C1 O2 N3 T4
   A5 C6 T7, so the expansion has 7 entries.
2. Decide what the target means in plain English, in one short sentence.
3. Build the phrase from that meaning. Each entry's word starts with its letter.
   The words must form a grammatical phrase in normal English word order,
   such as a noun phrase, adjective phrase, verb phrase, or short clause.
4. Read the finished phrase aloud. If it is nonsense, a list of unrelated
   words, or does not say what the target means, rewrite it.
5. Check before answering: the entry count equals the letter count; each entry's
   letter and word match target letter N; the target does not appear inside any
   word; no word is repeated.

RULES
1. Exactly one entry per letter of the target. Never more, never fewer.
2. Each entry's "letter" is the target letter at that position, and its "word"
   starts with that letter.
3. Each "word" is one common English word using letters only. No hyphens,
   spaces, digits, abbreviations, names, or foreign words. Function words
   (of, in, to, and, that, with, a, the, or) are allowed and often needed to
   make the phrase grammatical, as long as they start with the right letter.
4. The target must not appear inside any "word", including inside a longer
   word. "you" is inside "young" and "yourself", so neither may be used for
   "you". For 1- and 2-letter targets, only an identical word is banned.
5. Do not repeat a word within one expansion.
6. Describe the general, everyday meaning of the target in a phrase that reads
   naturally. Do not use a niche or technical sense. Do not just list synonyms,
   related categories, or unrelated words side by side. Every word must add
   meaning to the phrase. Use real grammar: a noun phrase (adjectives + head
   noun + what it does or is), a verb phrase, or a short clause.
   Do not use filler words such as everywhere, things, thing, stuff,
   interesting, important, various, specific, influence, influences, overall,
   general, related, or unique. They say nothing about the target.
7. Short, common words such as "a", "in", "of", "the", "you", and "will" are
   the hardest. Build a phrase that explains what the word means or how it is
   used. Do not force a loose synonym list.
8. When you get feedback about a rejected answer, fix every listed problem and
   return a complete new answer, not a partial fix.
9. Return only the JSON object. No notes and no extra fields.

EXAMPLES (valid outputs):
"cat"  -> {"word":"cat","expansion":[{"letter":"C","word":"Curious"},{"letter":"A","word":"Animal"},{"letter":"T","word":"Tracker"}]}
"well" -> {"word":"well","expansion":[{"letter":"W","word":"Wholesome"},{"letter":"E","word":"Energy"},{"letter":"L","word":"Lifts"},{"letter":"L","word":"Lives"}]}
"of"   -> {"word":"of","expansion":[{"letter":"O","word":"Originating"},{"letter":"F","word":"From"}]}
"the"  -> {"word":"the","expansion":[{"letter":"T","word":"Targeted"},{"letter":"H","word":"Highlighted"},{"letter":"E","word":"Entity"}]}

OUTPUT FORMAT:
{"word":"TARGET","expansion":[{"letter":"L1","word":"W1"},{"letter":"L2","word":"W2"}]}
One object per letter of TARGET, in order. Replace TARGET and fill in every entry.
"""


JUDGE_SYSTEM = """
You are a strict editor of acronym expansions. You get a target word and a
phrase. The phrase is built from entries, one word per letter of the target,
and the words should read as a grammatical phrase that clearly means the
target word.

Score the phrase from 1.00 to 10.00, with two decimal places. Use the whole
range and give exact scores, such as 6.32, 7.81, or 4.17. These are anchors,
not the only allowed values:
about 9.5 to 10 = a natural phrase that clearly and correctly means the target
about 7 = correct meaning and grammar, with minor clumsiness (the pass line)
about 5 = vague, partly wrong, or awkward
about 3 = wrong meaning or hard to follow
about 1 = nonsense, or a list of loosely related words

Do not default to round numbers like 5.00, 7.00, or 8.50. Place each score
exactly where the phrase falls between these anchors. Two phrases that are both
decent should get different scores, and the one with better words, grammar, or
precision gets the higher one. Never end a score in .00 unless the phrase is
truly at that exact level.

Be strict: a plain list of related words cannot score above 4.00.

Feedback: name the specific words that are wrong or weak, and say what would fix
them, in at most three sentences.

Reply with only JSON: {"score": 7.35, "feedback": "..."}
"""


# -------------------- HELPERS --------------------

class Rejected(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


def log_error(word, attempt, message, raw=""):
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        f.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"word={word!r} attempt={attempt}: {message}\n"
        )
        if raw:
            f.write(f"Raw response: {raw[:2000]}\n")
        f.write("\n")


def letters_label(target):
    return " ".join(
        f"{letter.upper()}{index}"
        for index, letter in enumerate(target.lower(), start=1)
    )


def clean_json_text(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    return text[start:end + 1]


def validate_result(raw_text, target):
    target_lower = target.lower()

    try:
        result = json.loads(clean_json_text(raw_text))
    except (ValueError, json.JSONDecodeError) as exc:
        raise Rejected([
            f"Your reply was not a valid JSON object ({exc}). "
            "Reply with only the JSON object."
        ])

    if not isinstance(result, dict):
        raise Rejected([
            "Reply with a JSON object that has 'word' and 'expansion' fields."
        ])

    problems = []

    if not isinstance(result.get("word"), str) or result["word"].lower() != target_lower:
        problems.append(f"The 'word' field must be exactly '{target_lower}'.")

    expansion = result.get("expansion")
    if not isinstance(expansion, list):
        raise Rejected(problems + ["'expansion' must be a JSON array."])

    expected = len(target_lower)
    if len(expansion) != expected:
        problems.append(
            f"You returned {len(expansion)} entries, but '{target_lower}' has "
            f"{expected} letters ({letters_label(target_lower)}). "
            f"Return exactly {expected} entries, one per letter."
        )

    words = []
    seen = set()

    for index, item in enumerate(expansion[:expected]):
        number = index + 1
        letter = target_lower[index]

        if isinstance(item, dict):
            given_letter = item.get("letter")
            raw_word = item.get("word")
            given = re.sub(r"[^a-z]", "", given_letter.lower())[:1] if isinstance(given_letter, str) else ""
            if given and given != letter:
                problems.append(
                    f"Entry {number} has letter '{given_letter.strip().upper()}', "
                    f"but target letter {number} is '{letter.upper()}'."
                )
        else:
            raw_word = item

        if not isinstance(raw_word, str) or not re.fullmatch(r"[A-Za-z]+", raw_word.strip()):
            problems.append(
                f"Entry {number} word {raw_word!r} must be one English word "
                "using letters only."
            )
            continue

        shown = raw_word.strip()
        word = shown.lower()

        if word[0] != letter:
            problems.append(
                f"Entry {number} word '{shown}' must start with "
                f"'{letter.upper()}' (target letter {number})."
            )

        if (
            (len(target_lower) >= 3 and target_lower in word)
            or (len(target_lower) < 3 and target_lower == word)
        ):
            problems.append(
                f"Entry {number} word '{shown}' contains the target "
                f"'{target_lower}'. Choose a different word."
            )

        if word in seen:
            problems.append(f"Entry {number} repeats '{shown}'.")

        if word in FILLER_WORDS:
            problems.append(
                f"Entry {number} word '{shown}' is filler. Choose a word that "
                "says something specific about the target."
            )

        seen.add(word)
        words.append(word)

    if problems:
        raise Rejected(problems)

    return {"word": target, "expansion": words}


def task_message(target):
    return (
        f"Target word: {target}\n"
        f"Letters: {letters_label(target)} "
        f"(exactly {len(target)} entries, in this order)."
    )


def attempt_history(history):
    lines = []
    for item in history:
        if item["score"] is None:
            verdict = "rejected by format check"
        else:
            verdict = f"judge scored {item['score']:.2f}/10"
        lines.append(
            f"Attempt {item['attempt']} ({verdict}): "
            f"{item['phrase'] or '(no usable answer)'}\n"
            f"  Feedback: {item['feedback'] or 'none'}"
        )
    return "\n".join(lines)


def feedback_message(target, problems, history):
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        "Your answer was rejected for these reasons:\n"
        f"{listed}\n\n"
        f"All attempts so far for '{target}':\n{attempt_history(history)}\n\n"
        f"Target letters: {letters_label(target)} "
        f"(exactly {len(target)} entries).\n"
        "Fix every problem from every attempt above, not only the latest one, "
        "and return the complete corrected JSON object only."
    )


def parse_object(raw):
    data = json.loads(clean_json_text(raw))
    if not isinstance(data, dict):
        raise ValueError("reply must be a JSON object")
    return data


def judge_expansion(target, words, stats):
    """Returns (score, feedback). Score is None if the judge could not answer."""
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": f"Target word: {target}\nPhrase: {' '.join(words)}",
        },
    ]
    last_error = ""
    for _ in range(2):
        try:
            raw, body = ask_ollama(messages, JUDGE_TEMPERATURE, JUDGE_THINK)
            stats.eval_tokens += body.get("eval_count", 0)
            stats.eval_seconds += body.get("eval_duration", 0) / 1e9
            data = parse_object(raw)
            score = min(10.0, max(1.0, float(data["score"])))
            feedback = str(data.get("feedback", "")).strip()
            return round(score, 2), feedback
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, requests.RequestException) as exc:
            last_error = str(exc)
    return None, f"judge unavailable: {last_error}"


def ask_ollama(messages, temperature=0.4, think=THINK):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "stream": False,
            "think": think,
            "format": "json",
            "messages": messages,
            "options": {"temperature": temperature},
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    return body["message"]["content"], body


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
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def write_final_json(completed, original_words):
    # Preserve the original word-list order, then keep any other entries.
    ordered = [
        completed[word.lower()]
        for word in original_words
        if word.lower() in completed
    ]
    listed = {word.lower() for word in original_words}
    ordered += [entry for word, entry in completed.items() if word not in listed]
    temp_file = OUTPUT_FILE.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp_file.replace(OUTPUT_FILE)


# -------------------- RUN STATE --------------------

class RunStats:
    def __init__(self, total, already_done, todo):
        self.total = total
        self.already_done = already_done
        self.todo = todo
        self.started = time.time()
        self.processed = 0
        self.success = 0
        self.failed = 0
        self.discovered = 0
        self.grow = False
        self.discovery_history = deque(maxlen=PACE_WINDOW * 2)
        self._forecast = None
        self._forecast_key = None
        self.weak = 0
        self.attempts = 0
        self.retries = 0
        self.eval_tokens = 0
        self.eval_seconds = 0.0
        self.word_times = deque(maxlen=PACE_WINDOW)
        self.recent = deque(maxlen=RECENT_ROWS)
        self.current = None
        self.current_attempt = 0
        self.last_error = ""

    def elapsed(self):
        return time.time() - self.started

    def done(self):
        return self.already_done + self.processed

    def pace(self):
        if not self.word_times:
            return None
        return sum(self.word_times) / len(self.word_times)

    def eta_seconds(self):
        pace = self.pace()
        if pace is None:
            return None
        return pace * (self.todo - self.processed)

    def words_per_minute(self):
        elapsed = self.elapsed()
        if self.processed == 0 or elapsed <= 0:
            return 0.0
        return self.processed / elapsed * 60

    def tokens_per_second(self):
        if self.eval_seconds <= 0:
            return 0.0
        return self.eval_tokens / self.eval_seconds

    def forecast(self):
        key = (self.processed, self.todo)
        if self._forecast_key != key:
            self._forecast = project_remaining(self)
            self._forecast_key = key
        return self._forecast


def project_remaining(stats):
    """Forecast how many more words a snowball run will process.

    Each processed word adds about `rate` new words to the queue. The rate is
    taken from the recent window and shrinks by the trend seen between the last
    two windows, since new words get rarer as the known set grows.
    Returns None while there is too little data, or a dict with the sample
    count, the rate, and the remaining steps (None if the queue never empties).
    """
    history = list(stats.discovery_history)
    if len(history) < FORECAST_MIN_SAMPLES:
        return None

    recent = history[-PACE_WINDOW:]
    rate = sum(recent) / len(recent)
    earlier = history[-2 * PACE_WINDOW:-PACE_WINDOW]
    decay = 1.0
    if len(earlier) >= PACE_WINDOW // 2:
        prior = sum(earlier) / len(earlier)
        if prior > 0 and rate > 0:
            decay = min(1.0, max(0.95, (rate / prior) ** (1 / PACE_WINDOW)))

    queue = stats.todo - stats.processed
    steps = 0
    current = rate
    while queue >= 0.5:
        if steps >= FORECAST_MAX_STEPS:
            return {"samples": len(history), "rate": rate, "steps": None}
        queue += current - 1
        current *= decay
        steps += 1
    return {"samples": len(history), "rate": rate, "steps": steps}


# -------------------- DISPLAY --------------------

def stat_grid(rows):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(justify="right", style="bold", no_wrap=True)
    for label, value in rows:
        grid.add_row(label, value)
    return grid


def forecast_rows(stats, pace):
    forecast = stats.forecast()
    if forecast is None:
        return [(
            "Projected finish",
            f"collecting data ({len(stats.discovery_history)}/{FORECAST_MIN_SAMPLES})",
        )]
    if forecast["steps"] is None:
        return [("Projected finish", "not yet: queue keeps growing")]

    steps = forecast["steps"]
    total = stats.done() + steps
    seconds = pace * steps if pace is not None else None
    finish = (
        (datetime.now() + timedelta(seconds=seconds)).strftime("%a %b %d  %H:%M")
        if seconds is not None
        else "—"
    )
    return [
        ("New words per word", f"{forecast['rate']:.2f} (last {PACE_WINDOW})"),
        ("Projected total", f"{total:,} words"),
        ("Projected finish", finish),
        ("Projected left", f"{steps:,} words"),
    ]


def render(stats):
    done = stats.done()
    percent = done / stats.total * 100 if stats.total else 100
    eta = stats.eta_seconds()
    eta_text = format_duration(eta) if eta is not None else "estimating..."
    finish_text = (
        (datetime.now() + timedelta(seconds=eta)).strftime("%a %b %d  %H:%M")
        if eta is not None
        else "—"
    )
    pace = stats.pace()
    pace_text = f"{pace:.1f}s/word" if pace is not None else "—"
    success_rate = (
        stats.success / stats.processed * 100 if stats.processed else 0
    )

    title = Text.assemble(
        ("EWAA ", "bold magenta"),
        ("Acronym Expansion Generator", "bold"),
        ("   model ", "dim"),
        (MODEL, "cyan"),
        (f"{' · thinking' if THINK else ''}", "cyan"),
        ("   input ", "dim"),
        (f"{stats.todo:,} to do", "cyan"),
        (f" · {stats.already_done:,} resumed", "dim"),
    )

    progress = Panel(
        _progress_block(done, stats.total, percent),
        title="Overall progress",
        border_style="magenta",
        box=box.ROUNDED,
    )

    left = Panel(
        stat_grid([
            ("Success", f"{stats.success:,}  ({success_rate:.1f}%)"),
            ("Failed", f"[red]{stats.failed:,}[/red]" if stats.failed else "0"),
            ("Below 7 (kept)", f"[yellow]{stats.weak:,}[/yellow]" if stats.weak else "0"),
            ("New words added", f"{stats.discovered:,}"),
            ("Retries", f"{stats.retries:,}"),
            ("Attempts", f"{stats.attempts:,}"),
            ("Throughput", f"{stats.words_per_minute():.1f} words/min"),
            ("Recent pace", pace_text),
            ("Generation", f"{stats.tokens_per_second():.1f} tok/s"),
        ]),
        title="Stats",
        border_style="cyan",
        box=box.ROUNDED,
    )

    time_rows = [
        ("Elapsed", format_duration(stats.elapsed())),
        ("Queue ETA" if stats.grow else "ETA", eta_text),
        ("Queue finish" if stats.grow else "Finish (est.)", finish_text),
        ("Remaining", f"{stats.todo - stats.processed:,} words"),
    ]
    if stats.grow:
        time_rows += forecast_rows(stats, pace)

    right = Panel(
        stat_grid(time_rows),
        title="Time",
        border_style="cyan",
        box=box.ROUNDED,
    )

    if stats.current is not None:
        now_text = Text.assemble(
            ("Working on  ", "dim"),
            (repr(stats.current), "bold yellow"),
            (f"   attempt {stats.current_attempt}/{MAX_RETRIES + 1}", "dim"),
        )
    else:
        now_text = Text("Idle", style="dim")
    if stats.last_error:
        now_text.append("\nLast problem: ", style="dim")
        now_text.append(stats.last_error, style="red")
    current = Panel(now_text, title="Now", border_style="yellow", box=box.ROUNDED)

    recent = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        header_style="bold",
        show_edge=False,
    )
    recent.add_column("#", justify="right", style="dim", no_wrap=True)
    recent.add_column("Word", style="bold", no_wrap=True)
    recent.add_column("Expansion / error", ratio=1)
    recent.add_column("Tries", justify="right", no_wrap=True)
    recent.add_column("Time", justify="right", no_wrap=True)
    for index, target, detail, ok, tries, seconds in reversed(stats.recent):
        recent.add_row(
            f"{index:,}",
            target,
            Text(detail, style="green" if ok else "red"),
            str(tries),
            f"{seconds:.1f}s",
        )
    recent_panel = Panel(recent, title="Recent results", border_style="blue", box=box.ROUNDED)

    columns = Table.grid(expand=True)
    columns.add_column(ratio=1)
    columns.add_column(ratio=1)
    columns.add_row(left, right)

    stacked = Table.grid(expand=True)
    stacked.add_row(Panel(title, box=box.ROUNDED, border_style="magenta"))
    stacked.add_row(progress)
    stacked.add_row(columns)
    stacked.add_row(current)
    stacked.add_row(recent_panel)
    return stacked


def _progress_block(done, total, percent):
    width = max(20, shutil.get_terminal_size((100, 20)).columns - 12)
    filled = int(width * done / total) if total else width
    bar = Text.assemble(
        ("█" * filled, "magenta"),
        ("░" * (width - filled), "grey37"),
    )
    return Group(
        bar,
        Text.assemble(
            (f"{done:,}", "bold"),
            (f" / {total:,} words", "dim"),
            (f"   {percent:.2f}%", "bold magenta"),
        ),
    )


def print_summary(console, stats, output_path, checkpoint_path, error_path, failed_path, stopped):
    table = stat_grid([
        ("Status", "[yellow]Stopped (progress saved)[/yellow]" if stopped else "[green]Complete[/green]"),
        ("Processed this run", f"{stats.processed:,}"),
        ("Success", f"{stats.success:,}"),
        ("Failed", f"{stats.failed:,}"),
        ("Below 7 (kept as fallback)", f"{stats.weak:,}"),
        ("New words added", f"{stats.discovered:,}"),
        ("Retries", f"{stats.retries:,}"),
        ("Total elapsed", format_duration(stats.elapsed())),
        ("Average per word", f"{stats.elapsed() / stats.processed:.1f}s" if stats.processed else "—"),
        ("Generation speed", f"{stats.tokens_per_second():.1f} tok/s"),
        ("JSON output", str(output_path.resolve())),
        ("Checkpoint", str(checkpoint_path.resolve())),
        ("Error log", str(error_path.resolve())),
        ("Failed words", str(failed_path.resolve())),
    ])
    console.print(Panel(table, title="Run summary", border_style="magenta", box=box.ROUNDED))


# -------------------- MAIN --------------------

def review_message(target, history):
    latest = history[-1]
    return (
        f"A reviewer scored your last answer {latest['score']:.2f}/10 for '{target}' "
        f"(passing is {JUDGE_PASS:.2f}).\n\n"
        f"All attempts so far for '{target}':\n{attempt_history(history)}\n\n"
        "Rewrite the expansion. Fix the weaknesses from every attempt above, not only "
        "the latest one, and do not bring back phrasings that were already judged weak. "
        "Return the complete corrected JSON object only."
    )


def process_word(target, completed, stats):
    word_started = time.time()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_message(target)},
    ]
    accepted = None      # (entry, score) for an answer that passed
    best_weak = None     # (entry, score) for the best answer below JUDGE_PASS
    history = []         # Every try so far: attempt, phrase, score, feedback
    tries = 0

    for attempt in range(1, MAX_TRIES + 1):
        raw = ""
        tries = attempt
        stats.current = target
        stats.current_attempt = attempt
        stats.attempts += 1
        if attempt > 1:
            stats.retries += 1

        try:
            raw, body = ask_ollama(messages)
            stats.eval_tokens += body.get("eval_count", 0)
            stats.eval_seconds += body.get("eval_duration", 0) / 1e9
            entry = validate_result(raw, target)
        except Rejected as exc:
            problems = exc.problems
        except (requests.RequestException, KeyError, json.JSONDecodeError) as exc:
            problems = [f"Request failed: {exc}"]
        else:
            if not JUDGE_ENABLED:
                accepted = (entry, None)
                break

            score, feedback = judge_expansion(target, entry["expansion"], stats)
            if score is None:
                stats.last_error = f"{target!r}: {feedback}"
                log_error(target, attempt, feedback, raw)
                accepted = (entry, None)
                break

            entry["judge_score"] = score
            if score >= JUDGE_PASS:
                accepted = (entry, score)
                break

            if best_weak is None or score > best_weak[1]:
                best_weak = (entry, score)

            history.append({
                "attempt": attempt,
                "phrase": " ".join(entry["expansion"]),
                "score": score,
                "feedback": feedback,
            })
            summary = f"judge {score:.2f}/10: {feedback}"
            stats.last_error = f"{target!r} attempt {attempt}: {summary}"
            log_error(target, attempt, summary, raw)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": review_message(target, history)})
            continue

        summary = "; ".join(problems)
        stats.last_error = f"{target!r} attempt {attempt}: {summary}"
        log_error(target, attempt, summary, raw)

        history.append({
            "attempt": attempt,
            "phrase": None,
            "score": None,
            "feedback": summary,
        })
        if raw:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": feedback_message(target, problems, history)})

    seconds = time.time() - word_started
    stats.processed += 1
    stats.word_times.append(seconds)
    stats.current = None

    if accepted is not None:
        entry, score = accepted
        below = False
    elif best_weak is not None:
        entry, score = best_weak
        below = True
    else:
        entry, score, below = None, None, False

    if entry is not None:
        previous = completed.get(target.lower())
        kept_previous = (
            previous is not None
            and previous.get("judge_score") is not None
            and score is not None
            and score < previous["judge_score"]
        )
        if kept_previous:
            new_score = score
            entry, score = previous, previous["judge_score"]
            below = score < JUDGE_PASS
        else:
            completed[target.lower()] = entry
            with CHECKPOINT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stats.success += 1
        if below:
            stats.weak += 1
        detail = " ".join(part.capitalize() for part in entry["expansion"])
        if score is not None:
            detail += f"  [{score:.2f}/10{' below 7' if below else ''}]"
        if kept_previous:
            detail += f"  (kept: new try scored {new_score:.2f})"
        color_ok = not below
        stats.recent.append((stats.done(), target, detail, color_ok, tries, seconds))
    else:
        stats.failed += 1
        with FAILED_FILE.open("a", encoding="utf-8") as f:
            f.write(target + "\n")
        reason = stats.last_error or "no valid answer"
        stats.recent.append((stats.done(), target, f"FAILED: {reason}", False, tries, seconds))


# -------------------- MENU --------------------

SOURCE_LABELS = {
    "normal": "Normal (filtered-english-words.txt)",
    "expansion": "Expansion words (words used in expansions, not generated yet)",
    "snowball": "Snowball (expansion words, plus new words as they come up)",
}

SOURCE_ORDER = ("normal", "expansion", "snowball")

MENU_HINT = "↑/↓ move · type a letter or number to jump · Enter select · Esc back"
THRESHOLD_HINT = "↑/↓ change · type a number 0–10 · Enter redo these words · Esc back"

KEY_SEQUENCES = {
    "\x1b[A": "up", "\x1bOA": "up",
    "\x1b[B": "down", "\x1bOB": "down",
}

pending_keys = deque()


def split_keys(text):
    keys = []
    index = 0
    while index < len(text):
        for sequence, name in KEY_SEQUENCES.items():
            if text.startswith(sequence, index):
                keys.append(name)
                index += len(sequence)
                break
        else:
            char = text[index]
            index += 1
            if char == "\x1b":
                if index < len(text) and text[index] in "[O":
                    index += 1
                    while index < len(text) and not (text[index].isalpha() or text[index] == "~"):
                        index += 1
                    index += 1
                else:
                    keys.append("esc")
            elif char in "\r\n":
                keys.append("enter")
            elif char in "\x7f\x08":
                keys.append("backspace")
            elif char == "\x03":
                raise KeyboardInterrupt
            else:
                keys.append(char)
    return keys


def read_key():
    if not pending_keys:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            data = os.read(fd, 64)
            if data == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
                data += os.read(fd, 64)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        pending_keys.extend(split_keys(data.decode("utf-8", "ignore")))
    return pending_keys.popleft() if pending_keys else None


def draw_menu(console, title, labels, cursor, header, hint):
    console.clear()
    rows = Table.grid(padding=(0, 1))
    rows.add_column(width=2, no_wrap=True)
    rows.add_column(no_wrap=True)
    for index, label in enumerate(labels):
        selected = index == cursor
        number = f"{index + 1}." if index < 9 else ""
        rows.add_row(
            Text("›", style="bold magenta") if selected else Text(" "),
            Text(f"{number} {label}", style="bold reverse" if selected else ""),
        )
    body = Group(Text(header, style="dim"), Text(""), rows, Text(""), Text(hint, style="dim"))
    console.print(Panel(body, title=title, border_style="magenta", box=box.ROUNDED))


def choose(console, title, labels, header=""):
    """Returns the chosen index, or None if the user pressed Esc."""
    cursor = 0
    while True:
        draw_menu(console, title, labels, cursor, header, MENU_HINT)
        key = read_key()
        if key == "up":
            cursor = (cursor - 1) % len(labels)
        elif key == "down":
            cursor = (cursor + 1) % len(labels)
        elif key == "enter":
            return cursor
        elif key == "esc":
            return None
        elif key and key.isdigit() and 1 <= int(key) <= min(9, len(labels)):
            cursor = int(key) - 1
        elif key and key.isalpha():
            for index, label in enumerate(labels):
                if label.lower().startswith(key.lower()):
                    cursor = index
                    break


def pause(console, message):
    console.print(message)
    console.print("[dim]Press any key to continue.[/dim]")
    read_key()


def ollama_ready(console):
    try:
        requests.get("http://localhost:11434/api/tags", timeout=5).raise_for_status()
        return True
    except requests.RequestException:
        pause(console, "[red]Cannot reach Ollama at localhost:11434. Start Ollama and try again.[/red]")
        return False


def read_word_file(path):
    with path.open("r", encoding="utf-8") as f:
        return list(dict.fromkeys(
            line.strip() for line in f
            if line.strip() and re.fullmatch(r"[A-Za-z]+", line.strip())
        ))


def build_expansion_words(completed):
    counts = Counter()
    for entry in completed.values():
        for word in entry["expansion"]:
            word = word.lower()
            if re.fullmatch(r"[a-z]+", word):
                counts[word] += 1
    words = [
        word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if word not in completed
    ]
    EXPANSION_WORDS_FILE.write_text("\n".join(words) + "\n", encoding="utf-8")
    return words


def load_source_words(source, completed):
    if source == "normal":
        return read_word_file(INPUT_FILE)
    if not EXPANSION_WORDS_FILE.exists():
        build_expansion_words(completed)
    return read_word_file(EXPANSION_WORDS_FILE)


def settings_menu(console, completed, source):
    notice = ""
    while True:
        labels = [
            f"{SOURCE_LABELS[key]}{'  (selected)' if source == key else ''}"
            for key in SOURCE_ORDER
        ] + ["Rebuild expansion word list", "Back"]
        header = notice or f"{len(completed):,} words generated."
        choice = choose(console, "Settings", labels, header)
        if choice is None or choice == len(labels) - 1:
            return source
        if choice < len(SOURCE_ORDER):
            source = SOURCE_ORDER[choice]
            notice = ""
            if source != "normal":
                words = build_expansion_words(completed)
                notice = f"Saved {len(words):,} words to {EXPANSION_WORDS_FILE}."
        elif choice == len(SOURCE_ORDER):
            words = build_expansion_words(completed)
            notice = f"Saved {len(words):,} words to {EXPANSION_WORDS_FILE}."


def redo_counts(completed):
    bands = Counter()
    for entry in completed.values():
        score = entry.get("judge_score")
        if score is not None:
            bands[min(10, int(score))] += 1

    failed = []
    if FAILED_FILE.exists():
        lines = FAILED_FILE.read_text(encoding="utf-8").splitlines()
        failed = list(dict.fromkeys(
            word for word in (line.strip().lower() for line in lines)
            if word and word not in completed
        ))
    return bands, failed


def redo_targets(completed, failed, threshold):
    judged = sorted(
        (entry["judge_score"], word)
        for word, entry in completed.items()
        if entry.get("judge_score") is not None and int(entry["judge_score"]) <= threshold
    )
    return failed + [word for _, word in judged]


def threshold_rows(bands, failed):
    rows = []
    total = len(failed)
    for threshold in range(0, 11):
        band = len(failed) if threshold == 0 else bands.get(threshold, 0)
        total += band if threshold else 0
        rows.append((threshold, band, total))
    return rows


def pick_threshold(console, bands, failed):
    """Returns the chosen threshold, or None if the user pressed Esc."""
    threshold = 0
    typed = ""
    rows = threshold_rows(bands, failed)
    while True:
        console.clear()
        table = Table(box=box.SIMPLE_HEAVY, header_style="bold", show_edge=False)
        table.add_column("Threshold", justify="right")
        table.add_column("Judge scores in band")
        table.add_column("Words in band", justify="right")
        table.add_column("Redo total", justify="right")
        for value, band, total in rows:
            if value == 0:
                scope = "failed (no expansion)"
            elif value == 10:
                scope = "10.00"
            else:
                scope = f"{value}.00 – {value}.99"
            table.add_row(
                str(value), scope, f"{band:,}", f"{total:,}",
                style="bold reverse" if value == threshold else "",
            )
        redo_total = rows[threshold][2]
        caption = (
            f"Threshold {threshold} redoes {redo_total:,} words: "
            f"failed words plus judge scores below {threshold + 1}.00."
        )
        body = Group(table, Text(""), Text(caption), Text(""), Text(THRESHOLD_HINT, style="dim"))
        console.print(Panel(body, title="Redo judged results", border_style="magenta", box=box.ROUNDED))

        key = read_key()
        if key == "up":
            threshold = max(0, threshold - 1)
            typed = ""
        elif key == "down":
            threshold = min(10, threshold + 1)
            typed = ""
        elif key and key.isdigit():
            typed = (typed + key)[-2:]
            if int(typed) > 10:
                typed = key
            threshold = int(typed)
        elif key == "enter":
            return threshold
        elif key == "esc":
            return None


def discover_words(entry, completed, queue, known, stats):
    for word in entry["expansion"]:
        word = word.lower()
        if word in known or word in completed or not re.fullmatch(r"[a-z]+", word):
            continue
        known.add(word)
        queue.append(word)
        stats.total += 1
        stats.todo += 1
        stats.discovered += 1


def run_words(console, targets, order, completed, already_done, total, grow=False):
    stats = RunStats(total=total, already_done=already_done, todo=len(targets))
    stats.grow = grow
    queue = deque(targets)
    known = {target.lower() for target in targets}
    stopped = False
    try:
        with Live(
            get_renderable=lambda: render(stats),
            console=console,
            refresh_per_second=REFRESH_PER_SECOND,
            transient=False,
        ):
            while queue:
                target = queue.popleft()
                found_before = stats.discovered
                process_word(target, completed, stats)
                if grow and target.lower() in completed:
                    discover_words(completed[target.lower()], completed, queue, known, stats)
                if grow:
                    stats.discovery_history.append(stats.discovered - found_before)
    except KeyboardInterrupt:
        stopped = True
    finally:
        write_final_json(completed, order)

    print_summary(
        console,
        stats,
        OUTPUT_FILE,
        CHECKPOINT_FILE,
        ERROR_LOG,
        FAILED_FILE,
        stopped,
    )


def generate(console, completed, source):
    words = load_source_words(source, completed)
    todo = [word for word in words if word.lower() not in completed]
    if not todo:
        write_final_json(completed, read_word_file(INPUT_FILE))
        pause(console, "[green]Nothing left to do for this word source.[/green]")
        return
    if not ollama_ready(console):
        return
    run_words(
        console,
        todo,
        read_word_file(INPUT_FILE),
        completed,
        already_done=len(words) - len(todo),
        total=len(words),
        grow=source == "snowball",
    )
    pause(console, "[green]Run finished.[/green]")


def redo(console, completed):
    bands, failed = redo_counts(completed)
    threshold = pick_threshold(console, bands, failed)
    if threshold is None:
        return
    targets = redo_targets(completed, failed, threshold)
    if not targets:
        pause(console, "No words match that threshold.")
        return
    if not ollama_ready(console):
        return
    run_words(
        console,
        targets,
        read_word_file(INPUT_FILE),
        completed,
        already_done=0,
        total=len(targets),
    )
    pause(console, "[green]Redo finished.[/green]")


def main():
    console = Console()

    if not sys.stdin.isatty():
        raise SystemExit("Run this script in a terminal to use the menu.")
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Cannot find {INPUT_FILE}. Put it in this script's folder."
        )

    completed = load_completed()
    source = "normal"
    menu = ["Generate expansions", "Redo judged results", "Settings", "Quit"]

    try:
        while True:
            header = f"Source: {SOURCE_LABELS[source]} · {len(completed):,} generated"
            choice = choose(console, "EWAA · Acronym Expansion Generator", menu, header)
            if choice is None or choice == 3:
                break
            if choice == 0:
                generate(console, completed, source)
            elif choice == 1:
                redo(console, completed)
            else:
                source = settings_menu(console, completed, source)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
