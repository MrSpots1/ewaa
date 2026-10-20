
from datasets import load_dataset
import re

INPUT_FILE = "google-10000-english.txt"
OUTPUT_FILE = "filtered-english-words.txt"

# Load the English OpenList dataset
dataset = load_dataset(
    "ryanjosephkamp/english-openlist",
    split="train"
)

# Create a set of valid words
valid_words = {
    word.strip().lower()
    for word in dataset["text"]
    if word and re.fullmatch(r"[a-zA-Z]+", word.strip())
}

# Load the original Google list
with open(INPUT_FILE, "r", encoding="utf-8") as f:
    original_words = [
        line.strip()
        for line in f
        if line.strip()
    ]

# Keep only words present in the dictionary
filtered_words = [
    word for word in original_words
    if word.lower() in valid_words
    and re.fullmatch(r"[a-zA-Z]+", word)
]

for word in filtered_words:
    if len(word) < 2:
        print(f"Short word: {word}")

# Save the filtered list, preserving the original order
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(filtered_words) + "\n")

print(f"Original words: {len(original_words)}")
print(f"Valid words: {len(filtered_words)}")
print(f"Removed words: {len(original_words) - len(filtered_words)}")
print(f"Output: {OUTPUT_FILE}")
