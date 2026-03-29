import json
import random
import copy
from pathlib import Path

DATA_PATH = Path(__file__).parent / "../data/coding_questions.json"

with open(DATA_PATH, "r", encoding="utf-8") as f:
    HARD_QUESTIONS = json.load(f)

if not HARD_QUESTIONS:
    raise RuntimeError("coding_questions.json is empty")

_question_pool = HARD_QUESTIONS.copy()
random.shuffle(_question_pool)
_index = 0


def get_random_question():
    """Returns a non repeating randomized question"""
    global _index

    if _index >= len(_question_pool):
        random.shuffle(_question_pool)
        _index = 0

    q = copy.deepcopy(_question_pool[_index])
    _index += 1
    return fix_question(q)



def fix_question(question):
    """Randomizes options while keeping the correct answer accurate."""
    # Extract answer text.
    correct_letter = question["correct"]
    correct_idx = ord(correct_letter) - ord("a")

    correct_text = question["options"][correct_idx]

    random.shuffle(question["options"])

    question["correct"] = chr(
        question["options"].index(correct_text) + ord("a")
    )

    return question
