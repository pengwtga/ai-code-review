def handle_order(order):
    return order.id


def process_order(order):
    return handle_order(order)


def dispatch(orders):
    return [process_order(o) for o in orders]
