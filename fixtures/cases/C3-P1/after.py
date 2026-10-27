def load_config(path):
    conn = get_conn()
    if not path:
        return None
    return conn.query(path)
