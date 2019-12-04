from collections import Counter
file_path = "/Users/zacharypeng/Documents/11-731/assignment2/data/en_ts/ents_parallel.dev.en"

with open(file_path) as f:
    words = [l for l in f.read().split()]
    term_frequency = Counter(words)
    print(term_frequency)

