def save_config(cfg):
    try:
        write_file(cfg)
    except Exception:
        pass
