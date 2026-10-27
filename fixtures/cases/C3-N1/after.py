def load_config(path):
    with get_conn() as conn:
        if not path:
            return None
        return conn.query(path)
