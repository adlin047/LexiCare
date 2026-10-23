from __future__ import annotations

import csv
import json
import os
import queue
import re
import threading
import tkinter as tk

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tkinter import ttk, messagebox

import cmudict
import pyphen
import pyttsx3
import requests
from PIL import Image, ImageTk, UnidentifiedImageError

APP_TITLE = "LexiCare — Syllable & Visual Reader"
BASE_DIR = Path(__file__).resolve().parent

CORRECTIONS_FILE = BASE_DIR / "lexicare_corrections.json"
REPORTS_FILE = BASE_DIR / "lexicare_reports.csv"

PIXABAY_API_KEY = "Enter_Pixabay_API", "".strip()
PIXABAY_URL = "https://pixabay.com/api/"
REQUEST_TIMEOUT = 8
IMAGE_SIZE = (300, 260)

BACKGROUND = "#FAF3E0"
FOREGROUND = "#243047"
SYLLABLE_COLORS = ("#1769AA", "#C2413B")
HIGHLIGHT_COLOR = "#FDE68A"
FONT_FAMILY = "Verdana"

dictionary = pyphen.Pyphen(lang="en_US")
pronunciation_dictionary = cmudict.dict()

image_cache = {}
image_cache_lock = threading.Lock()


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


def save_corrections():
    temporary = CORRECTIONS_FILE.with_suffix(".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(
                corrections,
                file,
                ensure_ascii=False,
                indent=2,
            )

        temporary.replace(CORRECTIONS_FILE)
        return True

    except OSError as exc:
        messagebox.showerror(
            "Save failed",
            f"Could not save corrections:\n{exc}",
        )
        return False


def write_report(word, shown_parts, suggested_parts, reason):
    """Append a correction/error report to a local CSV file."""
    file_exists = REPORTS_FILE.exists()

    try:
        with REPORTS_FILE.open(
                "a",
                newline="",
                encoding="utf-8-sig",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "timestamp_utc",
                    "word",
                    "displayed_split",
                    "suggested_split",
                    "reason",
                ],
            )

            if not file_exists or REPORTS_FILE.stat().st_size == 0:
                writer.writeheader()

            writer.writerow({
                "timestamp_utc": datetime.now(
                    timezone.utc
                ).isoformat(timespec="seconds"),
                "word": word,
                "displayed_split": "-".join(shown_parts),
                "suggested_split": "-".join(suggested_parts),
                "reason": reason,
            })

        return True

    except OSError as exc:
        messagebox.showerror(
            "Report failed",
            f"Could not write the report:\n{exc}",
        )
        return False


corrections = load_corrections()


def fallback_split(word):
    """Use Pyphen as a fallback when pronunciation data is unavailable."""
    return dictionary.inserted(word).split("-")


def count_cmu_syllables(word):
    """Return a likely syllable count from CMUdict, or None."""
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
    """
    Generate candidate written boundaries.

    This is a heuristic, not a complete English phonics system.
    Pyphen candidates receive higher priority.
    """
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
    """Use a user's saved correction before automatic syllabification."""
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


class Speaker(threading.Thread):

    def __init__(self):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self.ready = threading.Event()
        self.voices = []
        self.error = None
        self.start()

    def run(self):
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 130)

            self.voices = engine.getProperty("voices") or []
            self.ready.set()

        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            print("Speech engine initialization failed:", exc)
            return

        while True:
            job = self.jobs.get()

            if job is None:
                break

            text, voice_id, rate = job

            try:
                if voice_id:
                    engine.setProperty("voice", voice_id)

                engine.setProperty("rate", rate)
                engine.say(text)
                engine.runAndWait()

            except Exception as exc:
                print("Speech error:", exc)

        try:
            engine.stop()
        except Exception:
            pass

    def say(self, text, voice_id, rate):
        if text.strip():
            self.jobs.put((text, voice_id, rate))


speaker = Speaker()


def get_voice_options():
    speaker.ready.wait(timeout=4)

    if speaker.error:
        return [("System default", "")]

    options = [("System default", "")]

    for voice in speaker.voices:
        name = getattr(voice, "name", None) or "Installed voice"
        voice_id = getattr(voice, "id", "")

        if voice_id:
            options.append((name, voice_id))

    return options


def fetch_image(word):
    key = word.lower()

    with image_cache_lock:
        if key in image_cache:
            return image_cache[key]

    if not PIXABAY_API_KEY:
        return None

    try:
        response = requests.get(
            PIXABAY_URL,
            params={
                "key": PIXABAY_API_KEY,
                "q": word,
                "image_type": "illustration",
                "safesearch": "true",
                "per_page": 3,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        hits = response.json().get("hits", [])

        if not hits:
            return None

        image_url = hits[0].get("webformatURL")

        if not image_url:
            return None

        image_response = requests.get(
            image_url,
            timeout=REQUEST_TIMEOUT,
        )
        image_response.raise_for_status()

        with Image.open(BytesIO(image_response.content)) as source:
            image = source.convert("RGB")
            image.thumbnail(IMAGE_SIZE)

        with image_cache_lock:
            image_cache[key] = image

        return image

    except (
            requests.RequestException,
            ValueError,
            OSError,
            UnidentifiedImageError,
    ) as exc:
        print("Image lookup failed:", exc)
        return None


state = {
    "words": [],
    "parts": [],
    "confidence": [],
    "source": [],
    "selected_index": None,
}

window = tk.Tk()
window.title(APP_TITLE)
window.geometry("760x900")
window.minsize(540, 680)
window.configure(bg=BACKGROUND)

voice_options = get_voice_options()


def current_voice_id():
    selected = voice_var.get()

    for name, voice_id in voice_options:
        if selected == name:
            return voice_id

    return ""


def current_rate():
    try:
        return int(rate_var.get())
    except (ValueError, tk.TclError):
        return 130


def speak(text):
    speaker.say(text, current_voice_id(), current_rate())


def highlight_word(index):
    text_display.tag_remove("active", "1.0", "end")
    ranges = text_display.tag_ranges(f"word_{index}")

    if ranges:
        text_display.tag_add("active", ranges[0], ranges[-1])


def render_words():
    text_display.config(state="normal")
    text_display.delete("1.0", "end")

    for index, word in enumerate(state["words"]):
        parts = state["parts"][index]
        confident = state["confidence"][index]
        source = state["source"][index]
        word_tag = f"word_{index}"

        for part_index, part in enumerate(parts):
            tags = [
                f"colour_{part_index % 2}",
                word_tag,
            ]

            if not confident and source != "user":
                tags.append("uncertain")

            text_display.insert("end", part, tuple(tags))

            if part_index < len(parts) - 1:
                text_display.insert("end", "·", (word_tag, "separator"))

        text_display.insert("end", "   ")

        text_display.tag_bind(
            word_tag,
            "<Button-1>",
            lambda event, i=index: select_word(i),
        )

        text_display.tag_bind(
            word_tag,
            "<Enter>",
            lambda event: text_display.config(cursor="hand2"),
        )

        text_display.tag_bind(
            word_tag,
            "<Leave>",
            lambda event: text_display.config(cursor="arrow"),
        )

    text_display.config(state="disabled")

    uncertain_count = sum(
        1
        for index, confident in enumerate(state["confidence"])
        if not confident and state["source"][index] != "user"
    )

    message = (
        f"{len(state['words'])} words · "
        f"{sum(len(parts) for parts in state['parts'])} displayed syllables"
    )

    if uncertain_count:
        message += f" · {uncertain_count} estimated splits underlined"

    status_label.config(text=message)


def select_word(index):
    if not 0 <= index < len(state["words"]):
        return

    state["selected_index"] = index
    word = state["words"][index]
    parts = state["parts"][index]

    highlight_word(index)
    selected_word_label.config(text=f"Selected: {word}")
    correction_entry.delete(0, "end")
    correction_entry.insert(0, "-".join(parts))
    reason_entry.delete(0, "end")

    speak(word)
    load_image(word)


image_request_number = 0


def show_placeholder(text):
    image_label.config(image="", text=text, width=28, height=12)
    image_label.image = None
    image_caption.config(text="")


def show_image(photo, caption):
    # width/height 0 = size the label to the image itself
    image_label.config(image=photo, text="", width=0, height=0)
    image_label.image = photo
    image_caption.config(text=caption)


def load_image(word):
    global image_request_number

    image_request_number += 1
    request_number = image_request_number

    if not PIXABAY_API_KEY:
        show_placeholder("Optional images require\na Pixabay API key.")
        return

    show_placeholder("Looking for an image...")

    def worker():
        image = fetch_image(word)

        def update_ui():
            if request_number != image_request_number:
                return

            if image is None:
                show_placeholder("No image found.")
            else:
                show_image(ImageTk.PhotoImage(image), word)

        window.after(0, update_ui)

    threading.Thread(target=worker, daemon=True).start()

    threading.Thread(target=worker, daemon=True).start()


def process_text(event=None):
    raw = input_entry.get().strip()

    if not raw:
        messagebox.showinfo(
            "LexiCare",
            "Please enter a word or sentence first.",
        )
        return

    words = tokenize(raw)

    if not words:
        messagebox.showinfo(
            "LexiCare",
            "No English words were found in that input.",
        )
        return

    results = [syllabify(word) for word in words]

    state["words"] = words
    state["parts"] = [result[0] for result in results]
    state["confidence"] = [result[1] for result in results]
    state["source"] = [result[2] for result in results]
    state["selected_index"] = None

    render_words()

    if len(words) == 1:
        select_word(0)
    else:
        speak(raw)


def clear_all():
    state["words"] = []
    state["parts"] = []
    state["confidence"] = []
    state["source"] = []
    state["selected_index"] = None

    text_display.config(state="normal")
    text_display.delete("1.0", "end")
    text_display.config(state="disabled")

    status_label.config(text="Enter a word or sentence to begin.")
    selected_word_label.config(text="No word selected")
    correction_entry.delete(0, "end")
    reason_entry.delete(0, "end")

    show_placeholder("Visual Representation")


def save_user_correction():
    index = state["selected_index"]

    if index is None:
        messagebox.showinfo(
            "Select a word",
            "Click a word in the reading area before correcting it.",
        )
        return

    word = state["words"][index]
    entered = correction_entry.get().strip()

    if not entered:
        messagebox.showwarning(
            "Missing correction",
            "Enter the syllable split, separating parts with hyphens.",
        )
        return

    # Remove spaces around each part but do not silently change spelling.
    parts = [part.strip() for part in entered.split("-")]

    if any(not part for part in parts):
        messagebox.showwarning(
            "Invalid split",
            "Do not leave an empty syllable between hyphens.",
        )
        return

    if "".join(parts).lower() != word.lower():
        messagebox.showwarning(
            "Spelling mismatch",
            "The parts must reconstruct the selected word exactly.\n\n"
            f"Selected word: {word}\n"
            f"Your parts: {''.join(parts)}",
        )
        return

    corrections[word.lower()] = parts

    if not save_corrections():
        corrections.pop(word.lower(), None)
        return

    state["parts"][index] = parts
    state["confidence"][index] = True
    state["source"][index] = "user"

    render_words()
    highlight_word(index)

    messagebox.showinfo(
        "Correction saved",
        f"Your split for '{word}' has been saved.\n"
        "LexiCare will reuse it next time.",
    )


def remove_user_correction():
    index = state["selected_index"]

    if index is None:
        messagebox.showinfo(
            "Select a word",
            "Click a word first.",
        )
        return

    word = state["words"][index]
    key = word.lower()

    if key not in corrections:
        messagebox.showinfo(
            "No saved correction",
            f"There is no saved correction for '{word}'.",
        )
        return

    previous = corrections.pop(key)

    if not save_corrections():
        corrections[key] = previous
        return

    parts, confident = automatic_split(word)

    state["parts"][index] = parts
    state["confidence"][index] = confident
    state["source"][index] = "automatic"

    render_words()
    highlight_word(index)


def report_problem():
    index = state["selected_index"]

    if index is None:
        messagebox.showinfo(
            "Select a word",
            "Click the word with the problem before reporting it.",
        )
        return

    word = state["words"][index]
    displayed_parts = state["parts"][index]
    suggested_text = correction_entry.get().strip()
    suggested_parts = [
        part.strip()
        for part in suggested_text.split("-")
        if part.strip()
    ]
    reason = reason_entry.get().strip()

    if "".join(suggested_parts).lower() != word.lower():
        suggested_parts = []

    success = write_report(
        word=word,
        shown_parts=displayed_parts,
        suggested_parts=suggested_parts,
        reason=reason,
    )

    if success:
        messagebox.showinfo(
            "Report saved",
            "Thanks! Your report was saved locally in:\n\n"
            f"{REPORTS_FILE}\n\n"
            "It has not been uploaded or sent automatically.",
        )


heading = tk.Label(
    window,
    text="LexiCare",
    font=(FONT_FAMILY, 22, "bold"),
    bg=BACKGROUND,
    fg=FOREGROUND,
)
heading.pack(pady=(12, 0))

subtitle = tk.Label(
    window,
    text="Syllable & Visual Reader",
    font=(FONT_FAMILY, 11),
    bg=BACKGROUND,
    fg="#666666",
)
subtitle.pack(pady=(0, 10))

tk.Label(
    window,
    text="Type a word or sentence:",
    font=(FONT_FAMILY, 11),
    bg=BACKGROUND,
).pack()

input_entry = tk.Entry(
    window,
    font=(FONT_FAMILY, 14),
    justify="center",
    relief="solid",
    bd=1,
)
input_entry.pack(fill="x", padx=20, pady=6, ipady=5)
input_entry.bind("<Return>", process_text)

top_buttons = tk.Frame(window, bg=BACKGROUND)
top_buttons.pack(pady=4)

ttk.Button(
    top_buttons,
    text="Process",
    command=process_text,
).pack(side="left", padx=4)

ttk.Button(
    top_buttons,
    text="Clear",
    command=clear_all,
).pack(side="left", padx=4)

voice_frame = tk.Frame(window, bg=BACKGROUND)
voice_frame.pack(pady=6)

tk.Label(
    voice_frame,
    text="Voice:",
    bg=BACKGROUND,
    font=(FONT_FAMILY, 9),
).pack(side="left", padx=(0, 4))

voice_names = [name for name, _ in voice_options]

voice_var = tk.StringVar(
    value=voice_names[0] if voice_names else "System default"
)

voice_menu = ttk.Combobox(
    voice_frame,
    textvariable=voice_var,
    values=voice_names,
    state="readonly",
    width=24,
)
voice_menu.pack(side="left", padx=(0, 12))

tk.Label(
    voice_frame,
    text="Speech rate:",
    bg=BACKGROUND,
    font=(FONT_FAMILY, 9),
).pack(side="left", padx=(0, 4))

rate_var = tk.StringVar(value="130")

rate_menu = ttk.Combobox(
    voice_frame,
    textvariable=rate_var,
    values=["90", "110", "130", "150", "170", "190", "210"],
    state="readonly",
    width=5,
)
rate_menu.pack(side="left")

content_frame = tk.Frame(window, bg=BACKGROUND)
content_frame.pack(fill="both", expand=True, padx=20, pady=(8, 0))

image_frame = tk.Frame(
    content_frame, bg=BACKGROUND, width=IMAGE_SIZE[0] + 20
)
image_frame.pack(side="right", fill="y", padx=(10, 0))
image_frame.pack_propagate(False)

image_label = tk.Label(
    image_frame,
    text="Visual representation",
    bg="#EFE6CF",
    font=(FONT_FAMILY, 10),
    width=28,
    height=12,
)
image_label.pack(pady=(10, 0))

image_caption = tk.Label(
    image_frame,
    text="",
    font=(FONT_FAMILY, 12, "bold"),
    bg=BACKGROUND,
    fg=FOREGROUND,
)
image_caption.pack(pady=(4, 0))

reading_frame = tk.Frame(content_frame, bg=BACKGROUND)
reading_frame.pack(side="left", fill="both", expand=True)

scrollbar = ttk.Scrollbar(reading_frame)
scrollbar.pack(side="right", fill="y")

text_display = tk.Text(
    reading_frame,
    wrap="word",
    font=(FONT_FAMILY, 21, "bold"),
    bg=BACKGROUND,
    fg=FOREGROUND,
    relief="flat",
    padx=6,
    pady=6,
    height=6,
    state="disabled",
    yscrollcommand=scrollbar.set,
)
text_display.pack(side="left", fill="both", expand=True)
scrollbar.config(command=text_display.yview)

text_display.tag_config(
    "colour_0",
    foreground=SYLLABLE_COLORS[0],
)
text_display.tag_config(
    "colour_1",
    foreground=SYLLABLE_COLORS[1],
)
text_display.tag_config(
    "separator",
    foreground="#777777",
)
text_display.tag_config(
    "uncertain",
    underline=True,
)
text_display.tag_config(
    "active",
    background=HIGHLIGHT_COLOR,
)

status_label = tk.Label(
    window,
    text="Enter a word or sentence to begin.",
    font=(FONT_FAMILY, 9),
    bg=BACKGROUND,
    fg="#555555",
    wraplength=700,
)
status_label.pack(pady=6)

correction_frame = tk.LabelFrame(
    window,
    text="Correct or report a syllable split",
    font=(FONT_FAMILY, 10, "bold"),
    bg=BACKGROUND,
    padx=10,
    pady=8,
)
correction_frame.pack(fill="x", padx=20, pady=6)

selected_word_label = tk.Label(
    correction_frame,
    text="No word selected",
    font=(FONT_FAMILY, 10, "bold"),
    bg=BACKGROUND,
    anchor="w",
)
selected_word_label.pack(fill="x", pady=(0, 4))

tk.Label(
    correction_frame,
    text="Correct split (separate syllables with hyphens):",
    bg=BACKGROUND,
    font=(FONT_FAMILY, 9),
    anchor="w",
).pack(fill="x")

correction_entry = tk.Entry(
    correction_frame,
    font=(FONT_FAMILY, 12),
)
correction_entry.pack(fill="x", pady=4)

correction_buttons = tk.Frame(
    correction_frame,
    bg=BACKGROUND,
)
correction_buttons.pack(fill="x", pady=3)

ttk.Button(
    correction_buttons,
    text="Save Correction",
    command=save_user_correction,
).pack(side="left", padx=(0, 5))

ttk.Button(
    correction_buttons,
    text="Remove Saved Correction",
    command=remove_user_correction,
).pack(side="left")

tk.Label(
    correction_frame,
    text="Optional explanation for the report:",
    bg=BACKGROUND,
    font=(FONT_FAMILY, 9),
    anchor="w",
).pack(fill="x", pady=(6, 0))

reason_entry = tk.Entry(
    correction_frame,
    font=(FONT_FAMILY, 10),
)
reason_entry.pack(fill="x", pady=4)

ttk.Button(
    correction_frame,
    text="Report Incorrect Split",
    command=report_problem,
).pack(anchor="w", pady=(2, 0))

input_entry.focus_set()

if __name__ == "__main__":
    window.mainloop()