from payment_gateway import PaymentGateway


class PaymentService:
    """Handles payment processing through the external gateway."""

    def __init__(self):
        self.gateway = PaymentGateway()

    def process(self, order: dict, payment_method: str) -> dict:
        """Charge the customer and return a payment receipt."""
        amount = sum(item.get("price", 0) * item.get("qty", 1) for item in order["items"])
        charge = self.gateway.charge(amount, payment_method)
        return {
            "payment_id": charge["id"],
            "order_id": order["id"],
            "amount": amount,
            "status": "completed",
        }
