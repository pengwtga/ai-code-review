def ratio(success, records):
    total = sum(r.amount for r in records)
    return success / total
