class Database:
    """In-memory stub that stands in for a real persistence layer."""

    def __init__(self):
        self._users: dict[str, dict] = {}
        self._orders: dict[str, dict] = {}

    def get_user(self, user_id: str) -> dict:
        return self._users[user_id]

    def get_user_by_username(self, username: str) -> dict | None:
        for user in self._users.values():
            if user["username"] == username:
                return user
        return None

    def save_order(self, order: dict) -> None:
        self._orders[order["id"]] = order

    def get_order(self, order_id: str) -> dict:
        return self._orders[order_id]

    def update_order_status(self, order_id: str, status: str) -> None:
        self._orders[order_id]["status"] = status
