def normalize(tag: str) -> str:
    return tag.strip().lower()


def add_tag(tag: str, tags=None):
    tags = tags if tags is not None else []
    tags.append(tag)
    return tags
