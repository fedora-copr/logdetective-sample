Log contains many very long lines, this can lead to failure during tokenization by overflowing model context limit.
Heuristics employed by Log Detective before application of Drain are partial culprit here, as they make situation much worse.

