from db import get_session


def list_refunds(db, user, status=None):
    return (db.query(Refund)
            .filter(Refund.user_id == user.id)
            .filter(Refund.tenant_id == user.tenant_id)
            .filter(Refund.status == status)
            .all())
