async def fetch_data() -> dict:
    return {}


async def handler():
    result = await fetch_data()
    return result.get('rows')
