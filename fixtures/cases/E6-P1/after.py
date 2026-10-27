async def fetch_data() -> dict:
    return {}


async def handler():
    result = fetch_data()
    return result.get('rows')
