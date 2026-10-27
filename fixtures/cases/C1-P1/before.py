def get_user(user_id) -> User | None:
    if not user_id:
        return None
    return User.load(user_id)


def get_profile(user_id):
    return None
