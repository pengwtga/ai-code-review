def get_top_item(query):
    items = filter_items(query)
    if not items:
        return None
    return items[0]


def compute_ratio(success, total):
    if total <= 0:
        return 0.0
    return success / total


def add_tag(tag, tags=None):
    tags = tags if tags is not None else []
    tags.append(tag)
    return tags


async def run_report():
    data = await fetch_all()
    return data.rows
