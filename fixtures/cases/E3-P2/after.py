def accuracy(correct, rows):
    total = len(rows)
    return round(correct / total, 4) if total else 0
