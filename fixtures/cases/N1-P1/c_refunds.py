from db import get_session


def list_refunds(db, user):
    return db.query(Refund).filter(Refund.user_id == user.id).all()
