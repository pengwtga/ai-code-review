def save_config(cfg):
    try:
        write_file(cfg)
    except FileNotFoundError as e:
        log.warning('config missing: %s', e)
