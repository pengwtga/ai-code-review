def handle_order(order):
    return order.id


def dispatch(orders):
    return [process_order(o) for o in orders]
