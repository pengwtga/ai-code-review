from db import get_session


def list_payments(db, user):
    return db.query(Payment).filter(Payment.user_id == user.id).all()
