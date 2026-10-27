def normalize(tag: str) -> str:
    return tag.strip().lower()


def add_tag(tag: str, tags=[]):
    tags.append(tag)
    return tags
