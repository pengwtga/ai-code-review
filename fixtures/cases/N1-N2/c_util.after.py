def format_money(cents, currency='CNY'):
    return f'{currency} {cents / 100:.2f}'
