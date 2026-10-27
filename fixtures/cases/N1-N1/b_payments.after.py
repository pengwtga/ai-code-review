from db import get_session


def list_payments(db, user):
    return (db.query(Payment)
            .filter(Payment.user_id == user.id)
            .filter(Payment.tenant_id == user.tenant_id)
            .all())
