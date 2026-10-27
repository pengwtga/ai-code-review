from db import get_session


def list_orders(db, user):
    return db.query(Order).filter(Order.user_id == user.id).all()
