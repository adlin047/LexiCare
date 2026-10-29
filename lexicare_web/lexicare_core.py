import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import cmudict
import pyphen

BASE_DIR = Path(__file__).resolve().parent
CORRECTIONS_FILE = BASE_DIR / "lexicare_corrections.json"
REPORTS_FILE = BASE_DIR / "lexicare_reports.csv"

dictionary = pyphen.Pyphen(lang="en_US")
pronunciation_dictionary = cmudict.dict()


def load_corrections():
    if not CORRECTIONS_FILE.exists():
        return {}

    try:
        with CORRECTIONS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {}

        valid = {}

        for word, parts in data.items():
            if (
                    isinstance(word, str)
                    and isinstance(parts, list)
                    and parts
                    and all(isinstance(part, str) and part for part in parts)
                    and "".join(parts).lower() == word.lower()
            ):
                valid[word.lower()] = parts

        return valid

    except (OSError, json.JSONDecodeError) as exc:
        print("Could not load corrections:", exc)
        return {}


corrections = load_corrections()


def save_corrections():
    temporary = CORRECTIONS_FILE.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(corrections, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(CORRECTIONS_FILE)
        return True
    except OSError as exc:
        print("Could not save corrections:", exc)
        return False


def write_report(word, shown_parts, suggested_parts, reason):
    is_new = not REPORTS_FILE.exists() or REPORTS_FILE.stat().st_size == 0
    try:
        with REPORTS_FILE.open("a", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "timestamp_utc", "word", "displayed_split",
                "suggested_split", "reason"])
            if is_new:
                writer.writeheader()
            writer.writerow({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "word": word,
                "displayed_split": "-".join(shown_parts),
                "suggested_split": "-".join(suggested_parts),
                "reason": reason,
            })
        return True
    except OSError as exc:
        print("Could not write report:", exc)
        return False


def fallback_split(word):
    return dictionary.inserted(word).split("-")

def count_cmu_syllables(word):
    pronunciations = pronunciation_dictionary.get(word.lower())

    if not pronunciations:
        return None

    counts = []

    for pronunciation in pronunciations:
        count = sum(
            bool(phone and phone[-1].isdigit())
            for phone in pronunciation
        )

        if count:
            counts.append(count)

    if not counts:
        return None

    frequencies = {
        count: counts.count(count)
        for count in set(counts)
    }

    return min(
        frequencies,
        key=lambda count: (-frequencies[count], count),
    )


def heuristic_boundaries(word):
    lower = word.lower()
    vowel_groups = []
    i = 0

    def is_vowel(index):
        char = lower[index]

        if char in "aeio":
            return True

        if char == "u":
            return not (
                    index > 0 and lower[index - 1] == "q"
            )

        return char == "y" and index > 0

    while i < len(lower):
        if is_vowel(i):
            start = i
            i += 1

            while i < len(lower) and is_vowel(i):
                i += 1

            vowel_groups.append((start, i))
        else:
            i += 1

    if len(vowel_groups) > 1:
        start, end = vowel_groups[-1]

        if (
                lower[start:end] == "e"
                and end == len(lower)
                and start > 0
                and lower[start - 1] != "l"
        ):
            vowel_groups.pop()

    candidates = set()

    common_pairs = {
        "bl", "br", "cl", "cr", "dr", "fl", "fr", "gl",
        "gr", "pl", "pr", "tr", "tw", "sk", "sl", "sm",
        "sn", "sp", "st", "sw", "th", "sh", "ch", "ph",
        "wh", "gh", "ck", "ng", "qu",
    }

    for (_, left_end), (right_start, _) in zip(
            vowel_groups, vowel_groups[1:]
    ):
        cluster = lower[left_end:right_start]

        if not cluster:
            position = left_end
        elif len(cluster) == 1:
            position = left_end
        elif len(cluster) == 2:
            position = (
                left_end
                if cluster in common_pairs
                else left_end + 1
            )
        else:
            position = left_end + 1

        if 0 < position < len(lower):
            candidates.add(position)

    return candidates


def automatic_split(word):
    if not word:
        return [], False

    if len(word) <= 3:
        return [word], True

    count = count_cmu_syllables(word)

    if count is None:
        parts = fallback_split(word)
        return parts, False

    if count <= 1:
        return [word], True

    pyphen_positions = set(dictionary.positions(word.lower()))
    heuristic_positions = heuristic_boundaries(word)

    scores = {}

    for position in pyphen_positions:
        if 0 < position < len(word):
            scores[position] = scores.get(position, 0) + 3

    for position in heuristic_positions:
        if 0 < position < len(word):
            scores[position] = scores.get(position, 0) + 1

    candidates = sorted(scores)
    required_cuts = count - 1

    if len(candidates) < required_cuts:
        return fallback_split(word), False

    dp = {}
    parent = {}

    def segment_penalty(piece):
        penalty = 0.0

        if not any(char in "aeiouy" for char in piece.lower()):
            penalty += 2.0

        if len(piece) == 1:
            penalty += 1.0

        return penalty

    for j, position in enumerate(candidates):
        first_piece = word[:position]

        dp[(1, j)] = (
                scores[position]
                - segment_penalty(first_piece)
                - 0.02 * abs(len(first_piece) - len(word) / count)
        )

        parent[(1, j)] = None

    for cuts in range(2, required_cuts + 1):
        for j, position in enumerate(candidates):
            best_score = None
            best_previous = None

            for i in range(j):
                previous_position = candidates[i]
                previous_score = dp.get((cuts - 1, i))

                if previous_score is None:
                    continue

                piece = word[previous_position:position]

                score = (
                        previous_score
                        + scores[position]
                        - segment_penalty(piece)
                        - 0.02 * abs(len(piece) - len(word) / count)
                )

                if best_score is None or score > best_score:
                    best_score = score
                    best_previous = i

            if best_previous is not None:
                dp[(cuts, j)] = best_score
                parent[(cuts, j)] = best_previous

    solutions = []

    for j, position in enumerate(candidates):
        score = dp.get((required_cuts, j))

        if score is None:
            continue

        score -= segment_penalty(word[position:])
        solutions.append((score, j))

    if not solutions:
        return fallback_split(word), False

    _, j = max(solutions, key=lambda item: item[0])

    selected = []
    cuts = required_cuts

    while cuts > 0:
        selected.append(candidates[j])
        previous = parent.get((cuts, j))

        if previous is None:
            break

        j = previous
        cuts -= 1

    selected.reverse()

    positions = [0] + selected + [len(word)]
    parts = [
        word[start:end]
        for start, end in zip(positions, positions[1:])
        if start < end
    ]

    valid = (
            len(parts) == count
            and "".join(parts).lower() == word.lower()
    )

    if not valid:
        return fallback_split(word), False

    return parts, True


def syllabify(word):
    saved = corrections.get(word.lower())
    if saved:
        parts = []
        offset = 0

        for saved_part in saved:
            length = len(saved_part)
            parts.append(word[offset:offset + length])
            offset += length

        if "".join(parts).lower() == word.lower():
            return parts, True, "user"

    parts, confident = automatic_split(word)
    return parts, confident, "automatic"


def tokenize(text):
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
