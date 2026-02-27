import uuid

from database import Database
from notifications import WarehouseNotifier


class OrderService:
    """Manages order lifecycle: creation, lookup, and status updates."""

    def __init__(self):
        self.db = Database()
        self.notifier = WarehouseNotifier()

    def create_order(self, user_id: str, items: list[dict]) -> dict:
        """Create a new order and notify the warehouse."""
        order = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "items": items,
            "status": "pending",
        }
        self.db.save_order(order)
        self.notifier.send_order(order)
        return order

    def get_order(self, order_id: str) -> dict:
        """Retrieve an order by ID."""
        return self.db.get_order(order_id)

    def mark_paid(self, order_id: str) -> None:
        """Update order status to paid after successful payment."""
        self.db.update_order_status(order_id, "paid")
